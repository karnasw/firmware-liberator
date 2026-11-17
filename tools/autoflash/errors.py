"""What stops a run, and what is worth trying again.
"""

from __future__ import annotations

import http.client
import re
import urllib.error


class Abort(RuntimeError):
    """What stops a run, saying what to do. `failure`, where given (or a
    subclass's own), is its class (failures.CLASSES), which its report sends
    as it is; without one its words are classed (failures.classify).
    tests/autoflash/test_failures.py holds each one's words to its class, as
    the API checks a report's words against the class it claims."""
    failure: str | None = None

    def __init__(self, *args: object, failure: str | None = None) -> None:
        super().__init__(*args)
        if failure is not None:
            self.failure = failure


class StoppedPartWay(Abort):
    """A send that stopped part way: what it got as far as writing is for the
    device to say (ATTEMPTED_EVIDENCE), so the step stays attempted."""
    failure = "upload-cut"


class NothingWritten(Abort):
    """A step stopped before it wrote anything to the device: a run of nothing
    else does not say what the device is (past_the_commit)."""


class Finished(NothingWritten):
    """The run ends here by the person's choice, with nothing converted: a
    readout instead (readout.py). Its message is what the run ends by saying."""


class GoBack(Exception):
    """A step finds that an earlier one did not take on the device - the image
    it sent was never installed: the run goes back and does that one again.
    `outcome`: what the stage line says of the step that found it."""

    def __init__(self, step: str, reason: str, outcome: str) -> None:
        super().__init__(reason)
        self.step, self.reason, self.outcome = step, reason, outcome


# A failure worth another go: the device or the network not answering.  A refusal
# - the device's checks, a password, the wrong model - would only say it again.
TRANSIENT_RE = re.compile(
    r"HTTP -1|timed out|timeout|sent nothing|did not answer|not answering|unreachable|"
    r"connection (refused|reset|aborted)|ConnectionError|IncompleteRead|URLError|"
    r"No route|stopped the \w+ without|did not complete within|Wi-Fi went down|"
    r"closed the connection|Broken pipe|connection to the (plug|device) was lost",
    re.IGNORECASE,
)


# The ways a request to the device fails on the network, as the step's own
# code did not catch them: a connection refused, reset or cut off, its reply
# cut short or garbled, or no reply in time.
NETWORK_ERRORS = (ConnectionError, TimeoutError, urllib.error.URLError, http.client.HTTPException)
