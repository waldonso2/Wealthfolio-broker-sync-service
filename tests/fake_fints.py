"""A fake python-fints client for the FinTS adapters (DKB and the profiles to come).

It answers like ``FinTS3PinTanClient`` from a recording (``ReplayClient``) and
adds what the tests need: an SCA request when the dialog starts, confirmed
after ``confirm_after`` polls, and a rejected PIN. The exception and response
classes carry python-fints' names: the adapter recognises them by name.
"""

from brokersync.adapters.fints import ReplayClient


class NeedTANResponse:  # same name as python-fints' class
    def __init__(self, decoupled=True, challenge="Bitte in der App freigeben."):
        self.decoupled = decoupled
        self.challenge = challenge


class FinTSClientPINError(Exception):
    pass


class FinTSClientTemporaryAuthError(Exception):
    pass


class FakeFinTS(ReplayClient):
    """A bank that wants a confirmation (decoupled, or a TAN to type) at the start of the dialog."""

    instances: list = []

    def __init__(self, recording, *, sca=True, decoupled=True, confirm_after=1, pin_ok=True, tan="123456"):
        super().__init__(recording)
        self.init_tan_response = NeedTANResponse(decoupled) if sca else None
        self.decoupled = decoupled
        self.confirm_after = confirm_after
        self.pin_ok = pin_ok
        self.tan = tan
        self.polls = 0
        self.calls = []
        FakeFinTS.instances.append(self)

    def __enter__(self):
        self.calls.append("enter")
        if not self.pin_ok:
            raise FinTSClientPINError("PIN wrong?")
        return self

    def send_tan(self, challenge, tan):
        self.polls += 1
        if self.decoupled and self.polls < self.confirm_after or not self.decoupled and tan != self.tan:
            return NeedTANResponse(self.decoupled)
        self.init_tan_response = None
        return "ok"

    def get_transactions(self, account, start_date=None, end_date=None):
        self.calls.append(("transactions", start_date))
        return super().get_transactions(account, start_date, end_date)
