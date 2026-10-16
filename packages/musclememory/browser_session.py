"""Host-owned browser capture, recall and verified learning through the Python SDK."""

from .browser import ground_browser_steps, record_browser_action, render_browser_advice
from .observer import ObservedRun


class BrowserMemorySession:
    """Connect an existing browser controller; never execute browser actions here.

    The host supplies real before/after observations and a domain verifier. Agent
    keys can learn/evaluate, but this class has no approval or trust-bypass API.
    """

    def __init__(self, client, intent, profile, namespace, parameters, *, spool_path=None):
        self.client, self.intent, self.parameters = client, intent, parameters
        self.run = ObservedRun(client, intent, profile, namespace, spool_path=spool_path)
        try:
            self.run.flush()
            self.recalled = client.musclememory_recall(
                intent_text=intent,
                runtime_profile=self.run.profile.model_dump(mode="json"),
                visibility_scope=namespace,
                match_limit=1,
                observation_run=self.run.run_token,
            )
        except BaseException:
            self.run.close()
            raise

    def guidance(self, state, *, budget=1000):
        if not self.recalled.get("candidates"):
            return ""
        definition = self.recalled["candidates"][0]["definition"]
        return render_browser_advice(
            ground_browser_steps(definition, self.parameters, state), budget
        )

    def record_action(
        self, action, target, before, after, *, value=None, duration_ms=0, permission=None
    ):
        return record_browser_action(
            self.run,
            action,
            target,
            before,
            after,
            value=value,
            duration_ms=duration_ms,
            permission=permission,
        )

    def complete(self, verifier, *, validation_target=None, learning_policy=None):
        """Call the HOST'S independent domain check, then learn or count a check.

        validation_target explicitly selects a procedure/version for an independent
        check. It is not automatically inferred merely because recall returned one.
        Failed checks flag that revision; successful new learning stays provisional.
        """
        check = self.run.instrument("browser.verify")(verifier)
        try:
            succeeded = check()
            if type(succeeded) is not bool:
                raise ValueError("Domain verifier must return a bool")
        except BaseException:
            self.run.complete(succeeded=False)
            raise
        evidence = self.run.events[-1]["event_id"]
        self.run.complete(succeeded=succeeded)
        if validation_target:
            return self.client.musclememory_evaluate(
                procedure_token=validation_target["procedure_token"],
                run_token=self.run.run_token,
                outcome_report={
                    "succeeded": succeeded,
                    "procedure_version": validation_target["procedure_version"],
                    "evidence_event_ids": [evidence],
                },
                performance_metrics={"observed": self.run.measurements()},
            )
        if not succeeded:
            return {"learned": False, "reason": "Domain verification failed"}
        types = {
            str: "string",
            int: "integer",
            float: "number",
            bool: "boolean",
            dict: "object",
            list: "array",
        }
        policy = {
            "title": self.intent,
            "parameters": {
                key: {"definition": {"kind": types[type(value)]}, "observed_value": value}
                for key, value in self.parameters.items()
            },
            **(learning_policy or {}),
        }
        return self.client.musclememory_learn(
            run_token=self.run.run_token,
            completion_proof={
                "succeeded": True,
                "evidence_event_ids": [evidence],
                "summary": "Host domain verifier passed",
            },
            learning_policy=policy,
        )

    def close(self):
        self.run.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
