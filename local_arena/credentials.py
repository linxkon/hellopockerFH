"""Internal-only credentials; official competition keys are never needed."""
import hashlib
import secrets


def fingerprint(key):
    return hashlib.sha256(key.encode()).hexdigest()


def new_key():
    return "sk_local_" + secrets.token_urlsafe(32)


def authenticate(header, participants):
    if not isinstance(header, str) or not header.startswith("Bearer "):
        return None
    digest = fingerprint(header[7:])
    for participant in participants:
        if secrets.compare_digest(digest, participant["key_hash"]):
            return participant["agent_id"]
    return None
