from cryptography.fernet import Fernet
from config import FERNET_KEY

_fernet = Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)


def encrypt(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    return _fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
