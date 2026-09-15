"""Typed delivery failures shared by upload and message delivery."""


class DeliveryError(Exception):
    def __init__(self, message, retryable=False, window=False, code='', category='', stage=''):
        super().__init__(('reply_window:' if window else '') + message)
        self.retryable, self.window = retryable, window
        self.code, self.category = str(code), category
        self.stage = stage
