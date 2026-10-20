"""Redis-backed ``StateStore`` + A2A ``MessageStore`` — state shared by every process.

Layout (all keys under ``<prefix>:``, default ``orch``)::

    job:<id>                 hash  job row (JSON-encoded complex fields)
    job:<id>:guidance        list  operator guidance not yet consumed
    job:<id>:events          list  stream events (capped)
    project:<pid>            hash  title, roe
    project:<pid>:jobs       zset  job ids by creation time
    project:<pid>:objectives hash  seq → JSON
    project:<pid>:findings   list  JSON rows        (+ :finding_keys set for dedupe)
    project:<pid>:prompts    list  JSON rows
    project:<pid>:assets|relations  list
    msg:<id> / inbox:<job_id> / pair:<a>:<b>   A2A messages
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from orchestrator.brain.protocols import Message
from orchestrator.host.state import JobState, Objective, ProjectMeta, Roe, Schedule, finding_key

_JSON_FIELDS = ("role_ids", "skill_ids", "extras")
_EVENT_CAP = 2000

# Atomic append of a deduped finding: KEYS = dedupe set, list; ARGV = key, row JSON without its closing brace.
# The seq is spliced in as text (cjson would turn empty arrays into objects). Returns the stored row JSON or false.
_ADD_FINDING = """
if redis.call('SADD', KEYS[1], ARGV[1]) == 0 then return false end
local seq = redis.call('LLEN', KEYS[2]) + 1
local sep = ', '
if ARGV[2] == '{' then sep = '' end
local out = ARGV[2] .. sep .. '"seq": ' .. seq .. '}'
redis.call('RPUSH', KEYS[2], out)
return out
"""

# Same for a plain list (checkpoints): KEYS = list; ARGV = row JSON without its closing brace. Returns the seq.
_ADD_SEQ = """
local seq = redis.call('LLEN', KEYS[1]) + 1
local sep = ', '
if ARGV[1] == '{' then sep = '' end
redis.call('RPUSH', KEYS[1], ARGV[1] .. sep .. '"seq": ' .. seq .. '}')
return seq
"""

# HSET only when the job exists (a patch must not create a partial job).
_PATCH_JOB = """
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
for i = 1, #ARGV, 2 do redis.call('HSET', KEYS[1], ARGV[i], ARGV[i + 1]) end
return 1
"""

# Atomic compare-and-set on the job's status field.
_CLAIM = """
local cur = redis.call('HGET', KEYS[1], 'status')
if not cur then return 0 end
for i = 1, #ARGV - 1 do
  if cur == ARGV[i] then
    redis.call('HSET', KEYS[1], 'status', ARGV[#ARGV])
    return 1
  end
end
return 0
"""


_UNLOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


def _client(url: str):
    import redis

    return redis.Redis.from_url(url, decode_responses=True)


def _open_json(row: dict[str, Any]) -> str:
    """A JSON object minus its closing brace, so a script can append a field."""
    return json.dumps(row, default=str)[:-1]


class RedisStateStore:
    def __init__(self, url: str = "redis://localhost:6379/0", *, prefix: str = "orch", client: Any = None) -> None:
        self.r = client or _client(url)
        self.p = prefix
        self._claim = self.r.register_script(_CLAIM)
        self._unlock = self.r.register_script(_UNLOCK)
        self._add_finding = self.r.register_script(_ADD_FINDING)
        self._add_seq = self.r.register_script(_ADD_SEQ)
        self._patch_job = self.r.register_script(_PATCH_JOB)

    def k(self, *parts: str) -> str:
        return ":".join((self.p, *parts))

    @contextmanager
    def lock(self, name: str, *, ttl: float = 30.0, wait: float = 30.0):
        """Named cross-process lock (SET NX PX + token-checked release). Not re-entrant."""
        key, token = self.k("lock", name), uuid.uuid4().hex
        deadline = time.monotonic() + wait
        while not self.r.set(key, token, nx=True, px=int(ttl * 1000)):
            if time.monotonic() > deadline:
                raise TimeoutError(f"could not acquire lock {name!r}")
            time.sleep(0.02)
        try:
            yield
        finally:
            self._unlock(keys=[key], args=[token])

    # projects
    def get_project(self, project_id: str) -> ProjectMeta:
        h = self.r.hgetall(self.k("project", project_id))
        roe = json.loads(h["roe"]) if h.get("roe") else None
        return ProjectMeta(project_id, h.get("title", ""), Roe.from_dict(roe) if h.get("roe") else None,
                           h.get("summary", ""), h.get("kind") or "timed",
                           h.get("plan_accepted", "1") != "0", h.get("status") or "active")

    def set_project(
        self, project_id: str, *, title: str = "", roe: Roe | None = None, summary: str = "",
        kind: str = "", plan_accepted: bool | None = None, status: str = "",
    ) -> ProjectMeta:
        mapping: dict[str, str] = {}
        if kind:
            mapping["kind"] = kind
        if plan_accepted is not None:
            mapping["plan_accepted"] = "1" if plan_accepted else "0"
        if status:
            mapping["status"] = status
        if title:
            mapping["title"] = title
        if summary:
            mapping["summary"] = summary
        if roe is not None:
            mapping["roe"] = json.dumps(roe.to_dict())
        key = self.k("project", project_id)
        if mapping:
            self.r.hset(key, mapping=mapping)
        else:
            self.r.hsetnx(key, "title", "")
        return self.get_project(project_id)

    # asset graph
    def get_graph(self, project_id: str) -> dict[str, Any] | None:
        raw = self.r.get(self.k("project", project_id, "graph"))
        return json.loads(raw) if raw else None

    def put_graph(self, project_id: str, graph: dict[str, Any]) -> None:
        self.r.set(self.k("project", project_id, "graph"), json.dumps(graph, default=str))

    # schedules
    def put_schedule(self, schedule: Schedule) -> None:
        self.r.set(self.k("schedule", schedule.id), json.dumps(schedule.__dict__))
        self.r.sadd(self.k("schedules"), schedule.id)

    def get_schedule(self, schedule_id: str) -> Schedule | None:
        raw = self.r.get(self.k("schedule", schedule_id))
        return Schedule(**json.loads(raw)) if raw else None

    def list_schedules(self, project_id: str = "") -> list[Schedule]:
        ids = list(self.r.smembers(self.k("schedules")))
        raws = self.r.mget([self.k("schedule", i) for i in ids]) if ids else []
        rows = [Schedule(**json.loads(x)) for x in raws if x]
        rows = [r for r in rows if not project_id or r.project_id == project_id]
        return sorted(rows, key=lambda r: (r.next_run_at or float("inf"), r.created_at))

    def patch_schedule(self, schedule_id: str, **fields: Any) -> None:
        with self.lock(f"schedule-row:{schedule_id}", ttl=5):
            s = self.get_schedule(schedule_id)
            if s is not None:
                for k, v in fields.items():
                    setattr(s, k, v)
                self.put_schedule(s)

    def delete_schedule(self, schedule_id: str) -> bool:
        self.r.srem(self.k("schedules"), schedule_id)
        return bool(self.r.delete(self.k("schedule", schedule_id)))

    # jobs
    @staticmethod
    def _row(job: JobState) -> dict[str, str]:
        out: dict[str, str] = {}
        for name, value in job.__dict__.items():
            if name == "events":
                continue
            out[name] = json.dumps(value) if name in _JSON_FIELDS else str(value)
        return out

    @staticmethod
    def _job(h: dict[str, str]) -> JobState:
        data: dict[str, Any] = dict(h)
        for name in _JSON_FIELDS:
            data[name] = json.loads(data.get(name) or ("{}" if name == "extras" else "[]"))
        data["objective_seq"] = int(data.get("objective_seq") or 0)
        data["depth"] = int(data.get("depth") or 1)
        data["created_at"] = float(data.get("created_at") or 0.0)
        data["heartbeat"] = float(data.get("heartbeat") or 0.0)
        data["control"] = data.get("control") or ""
        data["profile"] = data.get("profile") or ""
        return JobState(**data)

    def put_job(self, job: JobState) -> None:
        self.r.hset(self.k("job", job.id), mapping=self._row(job))
        self.r.zadd(self.k("project", job.project_id, "jobs"), {job.id: job.created_at})

    def get_job(self, job_id: str, *, events: bool = False) -> JobState | None:
        h = self.r.hgetall(self.k("job", job_id))
        if not h:
            return None
        job = self._job(h)
        if events:
            job.events = [json.loads(e) for e in self.r.lrange(self.k("job", job_id, "events"), 0, -1)]
        return job

    def list_projects(self) -> list[str]:
        prefix = self.k("project", "")
        keys = self.r.scan_iter(match=prefix + "*:jobs")
        return sorted(k[len(prefix):-len(":jobs")] for k in keys)

    def list_jobs(self, project_id: str) -> list[JobState]:
        ids = self.r.zrange(self.k("project", project_id, "jobs"), 0, -1)
        jobs = [self.get_job(i) for i in ids]
        return [j for j in jobs if j is not None]

    def patch_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        flat: list[str] = []
        for n, v in fields.items():
            flat += [n, json.dumps(v) if n in _JSON_FIELDS else str(v)]
        self._patch_job(keys=[self.k("job", job_id)], args=flat)

    def claim_job(self, job_id: str, from_statuses: set[str], to_status: str) -> bool:
        return bool(self._claim(keys=[self.k("job", job_id)], args=[*sorted(from_statuses), to_status]))

    def add_guidance(self, job_id: str, text: str) -> None:
        self.r.rpush(self.k("job", job_id, "guidance"), text)

    def pop_guidance(self, job_id: str) -> list[str]:
        key = self.k("job", job_id, "guidance")
        pipe = self.r.pipeline()
        pipe.lrange(key, 0, -1)
        pipe.delete(key)
        return list(pipe.execute()[0])

    def has_guidance(self, job_id: str) -> bool:
        return bool(self.r.llen(self.k("job", job_id, "guidance")))

    def add_event(self, job_id: str, event: dict[str, Any]) -> None:
        key = self.k("job", job_id, "events")
        self.r.rpush(key, json.dumps(event, default=str))
        self.r.ltrim(key, -_EVENT_CAP, -1)

    def add_checkpoint(self, job_id: str, checkpoint: dict[str, Any]) -> int:
        key = self.k("job", job_id, "checkpoints")
        return int(self._add_seq(keys=[key], args=[_open_json({k: v for k, v in checkpoint.items() if k != "seq"})]))

    def latest_checkpoint(self, job_id: str) -> dict[str, Any] | None:
        raw = self.r.lindex(self.k("job", job_id, "checkpoints"), -1)
        return json.loads(raw) if raw else None

    # objectives
    def get_objectives(self, project_id: str) -> list[Objective]:
        rows = self.r.hgetall(self.k("project", project_id, "objectives"))
        return sorted((Objective(**json.loads(v)) for v in rows.values()), key=lambda o: o.seq)

    def set_objectives(self, project_id: str, objectives: list[Objective]) -> None:
        key = self.k("project", project_id, "objectives")
        pipe = self.r.pipeline()
        pipe.delete(key)
        if objectives:
            pipe.hset(key, mapping={str(o.seq): json.dumps(o.__dict__) for o in objectives})
        pipe.execute()

    def patch_objective(self, project_id: str, seq: int, *, status: str, note: str = "") -> bool:
        key = self.k("project", project_id, "objectives")
        raw = self.r.hget(key, str(seq))
        if raw is None:
            return False
        o = json.loads(raw)
        o["status"], o["note"] = status, note
        self.r.hset(key, str(seq), json.dumps(o))
        return True


    # findings / prompts / assets
    def add_finding(self, project_id: str, row: dict[str, Any]) -> dict[str, Any] | None:
        out = self._add_finding(
            keys=[self.k("project", project_id, "finding_keys"), self.k("project", project_id, "findings")],
            args=[json.dumps(finding_key(row)), _open_json({k: v for k, v in row.items() if k != "seq"})])
        return json.loads(out) if out else None

    def get_findings(self, project_id: str) -> list[dict[str, Any]]:
        return [json.loads(x) for x in self.r.lrange(self.k("project", project_id, "findings"), 0, -1)]

    def add_prompt(self, project_id: str, prompt: dict[str, Any]) -> None:
        self.r.rpush(self.k("project", project_id, "prompts"), json.dumps(prompt))

    def get_prompts(self, project_id: str) -> list[dict[str, Any]]:
        return [json.loads(x) for x in self.r.lrange(self.k("project", project_id, "prompts"), 0, -1)]

    def set_prompt_answer(self, project_id: str, prompt_id: str, answer: str) -> dict[str, Any] | None:
        key = self.k("project", project_id, "prompts")
        for i, raw in enumerate(self.r.lrange(key, 0, -1)):
            p = json.loads(raw)
            if p["id"] == prompt_id:
                p["answer"] = answer
                self.r.lset(key, i, json.dumps(p))
                return p
        return None


class RedisMessageStore:
    """A2A ``MessageStore`` on Redis (so a message sent in one worker reaches another)."""

    def __init__(self, url: str = "redis://localhost:6379/0", *, prefix: str = "orch", client: Any = None) -> None:
        self.r = client or _client(url)
        self.p = prefix

    @staticmethod
    def _enc(m: Message) -> str:
        return json.dumps({
            "id": m.id, "sender_job_id": m.sender_job_id, "recipient_job_id": m.recipient_job_id,
            "kind": m.kind, "content": m.content, "created_at": m.created_at.isoformat(),
            "read_at": m.read_at.isoformat() if m.read_at else None, "reply_to": m.reply_to,
            "metadata": m.metadata,
        })

    @staticmethod
    def _dec(raw: str) -> Message:
        d = json.loads(raw)
        d["created_at"] = datetime.fromisoformat(d["created_at"])
        d["read_at"] = datetime.fromisoformat(d["read_at"]) if d.get("read_at") else None
        return Message(**d)

    def create_message(self, message: Message) -> Message:
        pipe = self.r.pipeline()
        pipe.set(f"{self.p}:msg:{message.id}", self._enc(message))
        for jid in (message.sender_job_id, message.recipient_job_id):
            pipe.zadd(f"{self.p}:inbox:{jid}", {message.id: message.created_at.timestamp()})
        pair = ":".join(sorted((message.sender_job_id, message.recipient_job_id)))
        pipe.incr(f"{self.p}:pair:{pair}")
        pipe.execute()
        return message

    def list_messages(self, *, job_id: str, since: datetime | None = None, unread_only: bool = False) -> list[Message]:
        ids = self.r.zrange(f"{self.p}:inbox:{job_id}", 0, -1)
        raws = self.r.mget([f"{self.p}:msg:{i}" for i in ids]) if ids else []
        out = [self._dec(x) for x in raws if x]
        if since is not None:
            out = [m for m in out if m.created_at >= since]
        if unread_only:
            out = [m for m in out if m.recipient_job_id == job_id and m.read_at is None]
        return sorted(out, key=lambda m: m.created_at)

    def mark_read(self, message_id: str) -> None:
        key = f"{self.p}:msg:{message_id}"
        raw = self.r.get(key)
        if raw:
            m = self._dec(raw)
            from dataclasses import replace

            from orchestrator.brain.protocols import utcnow

            self.r.set(key, self._enc(replace(m, read_at=utcnow())))

    def count_between(self, job_a: str, job_b: str) -> int:
        return int(self.r.get(f"{self.p}:pair:{':'.join(sorted((job_a, job_b)))}") or 0)
