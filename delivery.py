"""Per-event completion recorder for the reviewed AstrBot pipeline trace contract.

AstrBot's PipelineScheduler.execute is wrapped in trace_execution(trace).
trace_execution emits record("pipeline_end", status="success"|"error").
This only wraps this event instance, never patches AstrBot modules or global classes.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def attach_pipeline_ack(trace, inbox, event_id: str) -> None:
    """Attach an acknowledgement to exactly one AstrBot event's trace.

    A pipeline success means its stages returned normally, not that a message
    reached WeChat or that the language model generated a response.
    """
    original_record = trace.record
    delivered = False
    agent_response_missing = False

    def recorded(action: str, **fields):
        nonlocal delivered, agent_response_missing
        try:
            return original_record(action, **fields)
        finally:
            # AstrBot's run_agent() can catch a provider exception, set an
            # error result without yielding it to RespondStage, and let the
            # outer pipeline finish "successfully". Its final agent trace
            # then has no response. Treat that ambiguity as manual review.
            if action == "astr_agent_complete" and fields.get("resp") in (None, ""):
                agent_response_missing = True
            if action == "pipeline_end" and not delivered:
                delivered = True
                status = str(fields.get("status") or "")
                outcome = (
                    "agent_no_response"
                    if status == "success" and agent_response_missing
                    else status
                )
                try:
                    inbox.mark_pipeline_outcome(
                        event_id,
                        outcome=outcome,
                        error=str(fields.get("error") or ("pipeline_status_" + outcome))[:250],
                    )
                except Exception:
                    # A closed/unavailable DB must never corrupt AstrBot's own
                    # pipeline outcome. The state stays unacknowledged.
                    log.exception("Could not persist AstrBot pipeline result for %s", event_id)

    trace.record = recorded
