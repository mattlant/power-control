from __future__ import annotations

import hashlib
import hmac

from .models import Permission, Principal


class TokenAuthenticator:
    def __init__(self, credentials):
        self._records = {x.credential_id: x for x in credentials}

    def authenticate(self, header: str | None) -> Principal | None:
        if not header or not header.startswith("Bearer "): return None
        try:
            ident, secret = header[7:].split(".", 1)
        except ValueError:
            return None
        if not ident or not secret: return None
        record = self._records.get(ident)
        if record is None: return None
        digest = hashlib.scrypt(secret.encode(), salt=record.salt, n=16384, r=8, p=1, dklen=32)
        return Principal(record.credential_id, record.permissions) if hmac.compare_digest(digest,
                                                                                          record.scrypt_hash) else None


def authorize(principal: Principal, permission: Permission) -> bool: return permission in principal.permissions
