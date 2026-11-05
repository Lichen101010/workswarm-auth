"""Fixed, credential-safe errors shared by the client and server."""


class AuthError(Exception):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code, self.status = code, status
