"""Anona Memory for Softmax players.

Softmax runs competitive multi-agent leagues. A policy is a container: the
runner hands it a game socket, the episode ends, the container exits and is
never restarted. So a policy replays the same opponents for weeks and starts
every match knowing nothing.

This adapter is the exception to the shape every other adapter in this package
has. The rest wrap :class:`~anona.integrations.MemoryBridge` and recall and
store *around a model call*. A Softmax player cannot: its pod has **no outbound
network at all** — DNS does not resolve, and no proxy is offered (measured in a
hosted episode, 2026-09-11). It cannot reach Anona while it plays.

What it can reach is the in-cluster artifact endpoint. So memory leaves an
episode as an artifact, is ingested from outside the cluster, and comes back
into the next generation as an environment variable set at upload time:

    episode N  ->  artifact  ->  your machine / CI  ->  Anona  ->  episode N+1
    SoftmaxMemory                SoftmaxSync                      SoftmaxMemory

The two halves never share a process, and only one of them needs a network.
:class:`SoftmaxMemory` uses the standard library alone, so it stays usable in a
sealed pod; :class:`SoftmaxSync` drives the ordinary :class:`AnonaClient`.

Latency is what makes this workable rather than a compromise. Recall is read
once before the first action and written once before exit, never per tick —
retrieve is ~2.1s at p50, far outside a per-decision window — so the artifact
round trip costs a policy nothing it was going to use. What it gives up is
adapting to something first seen in the *current* episode.
"""
from __future__ import annotations

import io
import json
import os
import time
import urllib.error
import urllib.request
import zipfile
from typing import Any, Iterable

__all__ = ["SoftmaxMemory", "SoftmaxSync", "MEMORY_ENV", "ARTIFACT_NAME"]

MEMORY_ENV = "ANONA_MEMORY"
ARTIFACT_NAME = "anona-memory.json"
_ARTIFACT_URL_ENV = "COWORLD_PLAYER_ARTIFACT_UPLOAD_URL"
_OBSERVATORY = "https://softmax.com/api/observatory"


class SoftmaxMemory:
    """The in-episode half: read what earlier generations learned, record what
    this one did, write it to the player artifact on the way out.

    Standard library only, and it never attempts a network it cannot reach.

    Deliberately total: every method degrades to a no-op rather than raising,
    the same fail-open policy the framework adapters take. A policy that crashes
    because memory was unavailable is strictly worse than one that plays without
    it, and the middle of a league episode is not where you want to discover
    that an environment variable was missing.
    """

    def __init__(self, *, env: dict[str, str] | None = None, quiet: bool = False) -> None:
        self._env = dict(os.environ if env is None else env)
        self._quiet = quiet
        self._out: list[dict[str, Any]] = []
        self._started = time.time()
        self._recalled = self._load()

    def _load(self) -> dict[str, Any]:
        raw = self._env.get(MEMORY_ENV, "").strip()
        if not raw:
            self._say("no recall in env; this is generation 0")
            return {}
        try:
            blob = json.loads(raw)
        except json.JSONDecodeError:
            # A blob that will not parse is still evidence of intent, so keep it
            # readable rather than discarding it.
            self._say(f"recall present but not json ({len(raw)} chars)")
            return {"notes": [raw[:2000]], "generation": None}
        if not isinstance(blob, dict):
            return {"notes": [str(blob)]}
        self._say(f"recall present, generation {blob.get('generation')}, "
                  f"{len(blob.get('notes') or [])} notes")
        return blob

    def recall(self) -> list[str]:
        """What earlier generations learned. Empty on generation 0, which is the
        normal first run and not an error."""
        return [n for n in (self._recalled.get("notes") or []) if isinstance(n, str)]

    def recalled_raw(self) -> dict[str, Any]:
        """The whole blob, for a policy whose outside half compiles something
        richer than notes — tuned parameters, an opponent table."""
        return dict(self._recalled)

    @property
    def generation(self) -> int:
        """0 when nothing was recalled, else one past the recalled generation."""
        g = self._recalled.get("generation")
        return 0 if not isinstance(g, int) else g + 1

    def remember(self, observation: dict[str, Any] | str) -> None:
        """Queue one observation. Nothing leaves the pod until :meth:`flush`."""
        if isinstance(observation, str):
            observation = {"note": observation}
        self._out.append({"at": round(time.time() - self._started, 2), **observation})

    def remember_all(self, observations: Iterable[dict[str, Any] | str]) -> None:
        for o in observations:
            self.remember(o)

    def flush(self, *, extra: dict[str, Any] | None = None) -> bool:
        """Write the artifact. Call once, before the process exits.

        Returns whether it was accepted, and never raises: an episode that
        cannot save its memory should still finish and report its score.
        """
        url = self._env.get(_ARTIFACT_URL_ENV)
        if not url:
            self._say(f"no {_ARTIFACT_URL_ENV}; {len(self._out)} observations dropped")
            return False

        payload = {
            "schema": "anona.softmax.memory/1",
            "generation": self.generation,
            "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "pod": self._env.get("HOSTNAME"),
            "observations": self._out,
            **(extra or {}),
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(ARTIFACT_NAME, json.dumps(payload, indent=2))
        body = buf.getvalue()

        # The artifact channel has two shapes and a client that handles only one
        # loses memory silently in the other. A hosted episode sets an http URL
        # (`http://job-<id>-game:9091/player-artifact/...`); `coworld
        # run-episode` sets `file:///coworld-artifact/policy_artifact_0.zip`.
        # Local is where people iterate, so getting this wrong is worse than it
        # sounds — and note `file://` contains `://`, which defeats the obvious
        # "is this a URL" check.
        scheme, _, rest = url.partition("://")
        path = url if not rest else ("/" + rest.lstrip("/") if scheme == "file" else None)
        if path is not None:
            try:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "wb") as fh:
                    fh.write(body)
                self._say(f"artifact written to {path}, {len(body)} bytes, "
                          f"{len(self._out)} observations")
                return True
            except OSError as exc:
                self._say(f"artifact write failed: {type(exc).__name__}: {exc}")
                return False

        # Hosted: the endpoint takes PUT and answers 201 in about 100ms. Not
        # documented; established by trying PUT then POST in a live episode.
        req = urllib.request.Request(url, data=body, method="PUT",
                                     headers={"Content-Type": "application/zip"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                self._say(f"artifact {r.status}, {len(body)} bytes, "
                          f"{len(self._out)} observations")
                return 200 <= r.status < 300
        except (urllib.error.URLError, OSError) as exc:
            self._say(f"artifact upload failed: {type(exc).__name__}: {exc}")
            return False

    def _say(self, msg: str) -> None:
        if not self._quiet:
            print(f"[anona] {msg}", flush=True)


class SoftmaxSync:
    """The outside-the-cluster half: Observatory artifact -> Anona -> the blob
    the next generation is uploaded with.

    Needs a Softmax bearer token (``uv run softmax get-token``) and an Anona API
    key. Nothing here runs inside a pod.

    Scoping is one space per coworld with ``user_id`` set to the policy name,
    which makes the policy the subject the space accumulates knowledge about —
    ``get_user_profile(space, policy)`` is then a standing scouting report rather
    than something you assemble. Scoped reads are strict, so use the same
    convention for every write into a space or an earlier generation becomes
    invisible to later ones.
    """

    def __init__(
        self,
        *,
        softmax_token: str,
        anona_key: str,
        coworld: str,
        policy: str,
        space_id: str | None = None,
        base_url: str | None = None,
        observatory: str = _OBSERVATORY,
    ) -> None:
        import httpx

        from ..client import AnonaClient

        self.coworld = coworld
        self.policy = policy
        self.space_id = space_id or f"softmax-{coworld}"
        self._observatory = observatory.rstrip("/")
        self._sm = {"Authorization": f"Bearer {softmax_token}"}
        self._http = httpx.Client(timeout=60.0, follow_redirects=True)
        self._anona = AnonaClient(api_key=anona_key, **({"base_url": base_url} if base_url else {}))
        self._last_job: str | None = None

    # -- Observatory -------------------------------------------------------

    def fetch_artifact(self, episode_request_id: str, policy_version_id: str,
                       agent_index: int = 0) -> dict[str, Any] | None:
        """Pull one seat's artifact and return the memory payload inside it.

        Artifacts survive a *failed* episode — verified — which matters, because
        a policy that crashed is exactly the one whose memory you want.
        """
        url = (f"{self._observatory}/v2/episode-requests/{episode_request_id}"
               f"/{policy_version_id}/policy-artifact/{agent_index}")
        r = self._http.get(url, headers=self._sm)
        if r.status_code != 200:
            return None
        try:
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                return json.loads(z.read(ARTIFACT_NAME))
        except (zipfile.BadZipFile, KeyError, json.JSONDecodeError):
            return None

    def episode_meta(self, episode_request_id: str) -> dict[str, Any]:
        r = self._http.get(f"{self._observatory}/v2/episode-requests/{episode_request_id}",
                           headers=self._sm)
        return r.json() if r.status_code == 200 else {}

    # -- Anona -------------------------------------------------------------

    def ingest_episode(self, episode_request_id: str, policy_version_id: str,
                       agent_index: int = 0) -> int:
        """Artifact -> prose -> Anona. Returns how many observations were stored.

        The episode's own completion time rides as ``timestamp``, which is event
        time rather than ingest time. That is what lets ``occurred_after`` /
        ``occurred_before`` bound a window later instead of the window having to
        be spelled out in the prose of every memory.
        """
        payload = self.fetch_artifact(episode_request_id, policy_version_id, agent_index)
        if not payload:
            return 0
        meta = self.episode_meta(episode_request_id)
        when = meta.get("completed_at") or meta.get("created_at")
        variant = meta.get("variant_name") or self.coworld
        generation = payload.get("generation")

        items = []
        for obs in payload.get("observations") or []:
            bits = ", ".join(f"{k} {v}" for k, v in obs.items() if k != "at")
            items.append({
                "content": (f"In a Softmax {variant} episode, generation {generation} of policy "
                            f"'{self.policy}' observed: {bits}."),
                "metadata": {
                    "source": "softmax-artifact",
                    "coworld": self.coworld,
                    "policy": self.policy,
                    "generation": str(generation),
                    "episode_request": episode_request_id,
                },
                **({"timestamp": when} if when else {}),
            })
        if not items:
            return 0

        result = self._anona.record_batch(self.space_id, items[:100])
        self._last_job = (result or {}).get("job_id")
        return len(items[:100])

    def wait_for_ingest(self, job_id: str | None = None, *, timeout: float = 600.0) -> str:
        """Block until a queued write is extracted, and therefore recallable.

        ``record_batch`` is always queued and extraction is one model call per
        chunk, so a :meth:`compile` issued straight after :meth:`ingest_episode`
        recalls nothing and the space looks broken when it is merely still
        working. That is the single easiest way to misuse this adapter, so
        waiting is offered here rather than left to be discovered.
        """
        job_id = job_id or self._last_job
        if not job_id:
            return "no-job"
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                status = (self._anona.get_job(self.space_id, job_id) or {}).get("status")
            except Exception:  # noqa: BLE001 - polling must not raise into a pipeline
                status = None
            if status in ("completed", "failed", "cancelled"):
                return str(status)
            time.sleep(10)
        return "timeout"

    def compile(self, query: str, *, limit: int = 8, max_bytes: int = 3000) -> str:
        """Recall, packed into a blob small enough to ride ``--secret-env``.

        Trimmed because this value becomes an environment variable on the next
        upload: a recall that will not fit is a *deploy* failure, not merely a
        large memory. The weakest match is dropped first.
        """
        results = self._anona.retrieve(self.space_id, query, limit=limit,
                                       user_id=self.policy, agent_id=self.coworld)
        notes = [r.get("content", "") for r in results if r.get("content")]
        generation = self._latest_generation(results)
        while notes:
            blob = json.dumps({"generation": generation, "notes": notes}, separators=(",", ":"))
            if len(blob.encode()) <= max_bytes:
                return blob
            notes.pop()
        return json.dumps({"generation": generation, "notes": []}, separators=(",", ":"))

    @staticmethod
    def _latest_generation(results: list[dict]) -> int:
        best = 0
        for hit in results:
            g = (hit.get("metadata") or {}).get("generation")
            if isinstance(g, str) and g.isdigit():
                best = max(best, int(g))
        return best

    def upload_args(self, query: str) -> list[str]:
        """The argv fragment for the next ``coworld upload-policy``::

            subprocess.run(["uv", "run", "coworld", "upload-policy", image,
                            "--name", policy, *sync.upload_args("how do I beat X?")])
        """
        return ["--secret-env", f"{MEMORY_ENV}={self.compile(query)}"]

    def close(self) -> None:
        self._http.close()
        self._anona.close()

    def __enter__(self) -> "SoftmaxSync":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
