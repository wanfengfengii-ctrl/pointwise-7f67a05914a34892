RAW_COUNT_UNIT = 'count'


def validate_amplitude(value: int) -> int:
    if type(value) is not int:
        raise ValueError('amplitude must be a signed integer count')
    return value


def encode_amplitude(value: int) -> bytes:
    validate_amplitude(value)
    size = max(1, (value.bit_length() + 8) // 8)
    return value.to_bytes(size, 'big', signed=True)


def decode_amplitude(body: bytes) -> int:
    if not isinstance(body, bytes) or not body:
        raise ValueError('amplitude body must be a nonempty byte string')
    return int.from_bytes(body, 'big', signed=True)


def absolute_amplitude(value: int) -> int:
    return abs(validate_amplitude(value))
