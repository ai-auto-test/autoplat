"""UDS diagnostics over DoIP using udsoncan and doipclient (Chapter 9)."""
from __future__ import annotations

from typing import Any

from autoplat.errors import TargetUnreachable
from autoplat.timeline import current_timeline

SESSIONS = {"default": 0x01, "programming": 0x02, "extended": 0x03}

NRC_NAMES = {
    0x10: "generalReject", 0x11: "serviceNotSupported", 0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat", 0x14: "responseTooLong", 0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect", 0x24: "requestSequenceError", 0x31: "requestOutOfRange",
    0x33: "securityAccessDenied", 0x35: "invalidKey", 0x36: "exceedNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired", 0x72: "generalProgrammingFailure",
    0x78: "requestCorrectlyReceived-ResponsePending", 0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
}


class DiagnosticNegativeResponse(AssertionError):
    """A positive response was expected; the ECU refused. Treated as a product-level observation."""

    def __init__(self, service: str, code: int):
        self.service, self.code = service, code
        super().__init__(f"{service}: negative response 0x{code:02X} {NRC_NAMES.get(code, 'unknown')}")


class UdsDoipDiagnostics:
    def __init__(self, ecu_ip: str, logical_address: int, *, client_logical_address: int = 0x0E00,
                 dids: dict[int, Any] | None = None, request_timeout_s: float = 5.0):
        import udsoncan
        import udsoncan.configs
        from doipclient import DoIPClient
        from doipclient.connectors import DoIPClientUDSConnector
        from udsoncan.client import Client

        self._udsoncan = udsoncan
        try:
            self._doip = DoIPClient(ecu_ip, logical_address, client_logical_address=client_logical_address)
        except OSError as exc:
            raise TargetUnreachable(f"DoIP {ecu_ip} (0x{logical_address:04X}) unreachable: {exc}") from exc
        config = dict(udsoncan.configs.default_client_config)
        config["data_identifiers"] = dids or {0xF189: udsoncan.AsciiCodec(16), 0xF190: udsoncan.AsciiCodec(17)}
        config["request_timeout"] = request_timeout_s
        self._client = Client(DoIPClientUDSConnector(self._doip), config=config)
        self._client.open()

    def _call(self, service: str, fn, *args):
        from udsoncan.exceptions import NegativeResponseException, TimeoutException

        tl = current_timeline()
        try:
            response = fn(*args)
        except NegativeResponseException as exc:
            code = exc.response.code
            if tl is not None:
                tl.record("adapter", "uds", service, nrc=f"0x{code:02X}", name=NRC_NAMES.get(code, "unknown"))
            raise DiagnosticNegativeResponse(service, code) from exc
        except TimeoutException as exc:
            if tl is not None:
                tl.record("adapter", "uds", service, error="timeout")
            raise TargetUnreachable(f"{service}: no response (transport timeout)") from exc
        if tl is not None:
            tl.record("adapter", "uds", service, result="positive")
        return response

    def change_session(self, session: str, *, timeout_s: float = 5.0) -> None:
        self._call("DiagnosticSessionControl", self._client.change_session, SESSIONS[session])

    def read_did(self, did: int, *, timeout_s: float = 5.0) -> Any:
        resp = self._call(f"ReadDataByIdentifier 0x{did:04X}", self._client.read_data_by_identifier, did)
        return resp.service_data.values[did]

    def read_dtcs(self, *, status_mask: int = 0xFF, timeout_s: float = 5.0) -> list[dict[str, Any]]:
        resp = self._call("ReadDTCInformation", self._client.get_dtc_by_status_mask, status_mask)
        return [{"code": f"{d.id:06X}", "status": d.status.get_byte_as_int()} for d in resp.service_data.dtcs]

    def clear_dtcs(self, *, timeout_s: float = 5.0) -> None:
        self._call("ClearDiagnosticInformation", self._client.clear_dtc, 0xFFFFFF)

    def tester_present(self, *, timeout_s: float = 2.0) -> None:
        self._call("TesterPresent", self._client.tester_present)

    def close(self) -> None:
        try:
            self._client.close()
        finally:
            self._doip.close()
