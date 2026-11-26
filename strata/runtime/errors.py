class VaultError(Exception):
    def __init__(self, code: str, message: str, status: int, /, **details: object):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "details": self.details}


def invalid(message: str, **details: object) -> VaultError:
    return VaultError("invalid_request", message, 422, **details)


def missing(kind: str, identifier: str) -> VaultError:
    return VaultError("not_found", f"{kind} does not exist", 404, identifier=identifier)


def conflict(message: str, **details: object) -> VaultError:
    return VaultError("conflict", message, 409, **details)


def unavailable(message: str, **details: object) -> VaultError:
    return VaultError("storage_unavailable", message, 503, **details)


def damaged(digest: str, reason: str) -> VaultError:
    return VaultError("object_unreadable", "Archive object failed verification", 409,
                      digest=digest, reason=reason)
