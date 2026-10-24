"""Durable, leased learning work. Hooks return before inference starts."""

import threading
from datetime import timedelta

from sqlalchemy import select, update

from .core import DomainError
from .learning import PIPELINE
from .security import Principal
from .storage import Credential, LearningJob, utcnow


class LearningQueue:
    def __init__(self, service):
        self.service = service
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.lock = threading.Lock()

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.stop.clear()
            self.thread = threading.Thread(target=self.run, name="memory-learning", daemon=True)
            self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=1)

    def enqueue(self, principal, run_token, hint=None):
        service = self.service
        with service.db.transaction() as session:
            run = service._run(session, principal, run_token, "write")
            if run.status != "success":
                raise DomainError("unverified_run", "Only successful closed runs can be queued")
            if hint:
                procedure = service._procedure(session, principal, hint, "write")
                if procedure.namespace != run.namespace:
                    raise DomainError("learning_scope", "Hint belongs to another scope")
            job = session.scalar(
                select(LearningJob).where(
                    LearningJob.tenant == principal.tenant,
                    LearningJob.run_token == run_token,
                    LearningJob.pipeline == PIPELINE,
                )
            )
            if job is None:
                job = LearningJob(
                    tenant=principal.tenant,
                    namespace=run.namespace,
                    run_token=run_token,
                    pipeline=PIPELINE,
                    actor_key=principal.key_id,
                    hint=hint,
                )
                session.add(job)
            elif job.status == "complete":
                return job.result
            elif job.status == "error":
                job.status, job.available_at, job.attempts = "queued", utcnow(), 0
                job.actor_key = principal.key_id
            session.commit()
        self.start()
        self.wake.set()
        return {"action": "queued", "run_token": run_token}

    def claim(self):
        now = utcnow()
        with self.service.db.transaction() as session:
            candidate = session.scalar(
                select(LearningJob)
                .where(
                    LearningJob.pipeline == PIPELINE,
                    LearningJob.status.in_(["queued", "running"]),
                    LearningJob.available_at <= now,
                )
                .order_by(LearningJob.available_at, LearningJob.job_id)
                .limit(1)
            )
            if candidate is None:
                return None
            lease = now + timedelta(seconds=2 * self.service.settings.learning_timeout + 120)
            claimed = session.execute(
                update(LearningJob)
                .where(
                    LearningJob.job_id == candidate.job_id,
                    LearningJob.status == candidate.status,
                    LearningJob.available_at == candidate.available_at,
                )
                .values(status="running", available_at=lease, attempts=LearningJob.attempts + 1)
            )
            if not claimed.rowcount:
                return None
            identifier = candidate.job_id
            session.commit()
            return identifier

    def process(self, identifier):
        service = self.service
        with service.db.session() as session:
            job = session.get(LearningJob, identifier)
            credential = session.get(Credential, job.actor_key)
            if not credential or credential.revoked or credential.tenant != job.tenant:
                result = {"action": "cancelled", "reason": "credential_revoked"}
                principal = None
            else:
                principal = Principal(credential.key_id, credential.tenant, credential.grants)
                run_token, hint = job.run_token, job.hint
        if principal:
            try:
                result = service.auto_learn(principal, run_token, hint)
            except DomainError as error:
                result = {"action": "deferred", "reason": error.code}
            except Exception:
                result = {"action": "deferred", "reason": "learning_internal"}
        with service.db.transaction() as session:
            job = session.get(LearningJob, identifier)
            job.result = result
            if result["action"] == "deferred":
                job.status = "queued" if job.attempts < 3 else "error"
                job.available_at = utcnow() + timedelta(seconds=60 * 2 ** min(job.attempts, 5))
            else:
                job.status = "complete"
            session.commit()

    def run(self):
        while not self.stop.is_set():
            try:
                identifier = self.claim()
                if identifier:
                    self.process(identifier)
                    continue
            except Exception:
                # Storage may briefly be busy or shutting down. The lease and the
                # stored job survive; a later worker can retry the same source.
                pass
            self.wake.wait(timeout=5)
            self.wake.clear()
