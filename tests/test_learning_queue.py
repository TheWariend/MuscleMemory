from dataclasses import replace
from datetime import timedelta

from musclememory.core import DomainError
from musclememory.learning_queue import LearningQueue
from musclememory.storage import Credential, LearningJob, utcnow
from sqlalchemy import select
from test_check_evidence import FIX, edit_session
from test_semantic_learning import Learner, accepted, proposal


def queued(service, principal, monkeypatch):
    edit_session(service, principal, "source", [FIX])
    monkeypatch.setattr(service.learning_queue, "start", lambda: None)
    return service.learning_queue.enqueue(principal, "source")


def test_hook_acknowledges_durable_work_before_running_inference(
    service, principal, http, monkeypatch
):
    service.settings = replace(service.settings, learning_background=True)
    edit_session(service, principal, "source", [FIX])
    service.learner = Learner(lambda e: (proposal(e), accepted()))
    monkeypatch.setattr(service.learning_queue, "start", lambda: None)
    result = http.post("/v1/runs/source/auto-learn").json()
    assert result == {"action": "queued", "run_token": "source"}
    assert service.learner.calls == 0
    with service.db.session() as db:
        assert db.scalar(select(LearningJob)).status == "queued"
    job = service.learning_queue.claim()
    service.learning_queue.process(job)
    assert service.learner.calls == 1
    assert service.learning_queue.enqueue(principal, "source")["action"] == "learned"
    assert service.learning_queue.claim() is None


def test_lease_survives_worker_restart_and_prevents_duplicate_claims(
    service, principal, monkeypatch
):
    queued(service, principal, monkeypatch)
    first = service.learning_queue.claim()
    replacement = LearningQueue(service)
    assert replacement.claim() is None
    with service.db.transaction() as db:
        db.get(LearningJob, first).available_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert replacement.claim() == first
    assert replacement.claim() is None


def test_revoking_the_owner_key_cancels_queued_learning(service, principal, monkeypatch):
    queued(service, principal, monkeypatch)
    service.learner = Learner(lambda e: (proposal(e), accepted()))
    job = service.learning_queue.claim()
    with service.db.transaction() as db:
        db.get(Credential, principal.key_id).revoked = True
        db.commit()
    service.learning_queue.process(job)
    with service.db.session() as db:
        result = db.get(LearningJob, job)
        assert result.status == "complete" and result.result["reason"] == "credential_revoked"
    assert service.learner.calls == 0


def test_inference_outages_are_backed_off_and_not_lost(service, principal, monkeypatch):
    queued(service, principal, monkeypatch)

    def fail(evidence):
        raise DomainError("learning_timeout", "Temporary outage")

    service.learner = Learner(fail)
    job = service.learning_queue.claim()
    service.learning_queue.process(job)
    assert service.learning_queue.claim() is None
    with service.db.session() as db:
        stored = db.get(LearningJob, job)
        assert stored.status == "queued" and stored.result["reason"] == "learning_timeout"
    assert service.get_run(principal, "source")["status"] == "success"
