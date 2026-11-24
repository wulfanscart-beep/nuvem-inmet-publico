"""Versioned little-endian formats with fixed offsets for hourly data."""

from __future__ import annotations

import calendar
import hashlib
import os
import struct
from collections.abc import Iterable, Iterator
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import BinaryIO

ObservationValues = tuple[Decimal | None, Decimal | None]
ObservationSlot = ObservationValues | None
StationRecord = tuple[int, str, float | None, float | None]

_VERSION = 1
_OBSERVATION_MAGIC = b"NIMO"
_STATION_MAGIC = b"NIMS"
_OBSERVATION_HEADER = struct.Struct("<4sBIqII")
_STATION_HEADER = struct.Struct("<4sBI8s8sH")
_FLOAT64 = struct.Struct("<d")
_COORDINATE_NULL = bytes.fromhex("000000000000f87f")
_HOUR = struct.Struct("<hB")
_PRECISE_INDEX = struct.Struct("<IQI")
_EPOCH = date(1970, 1, 1)

_TEMP_NULL = -32768
_TEMP_PRECISE = -32767
_HUMIDITY_NULL = 255
_HUMIDITY_PRECISE = 254
_HUMIDITY_NULL_ROW = 253
_GAP = _HOUR.pack(_TEMP_NULL, _HUMIDITY_NULL)
_NULL_ROW = _HOUR.pack(_TEMP_NULL, _HUMIDITY_NULL_ROW)
_READ_CHUNK = 256


class FormatError(ValueError):
    """A binary file is incompatible, incomplete or invalid."""


def hours_in_year(year: int) -> int:
    """Return the number of hourly positions in a Gregorian UTC year."""
    date(year, 1, 1)
    return (365 + calendar.isleap(year)) * 24


def sha256_file(path: str | Path) -> str:
    """Hash a complete binary file with bounded memory use."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    data = stream.read(size)
    if len(data) != size:
        raise FormatError("Arquivo binário truncado")
    return data


def _check_station_id(station_id: int) -> None:
    if not 1 <= station_id <= 0xFFFFFFFF:
        raise ValueError("Identificador interno fora do intervalo UInt32 positivo")


def _encode_coordinate(value: float | None, limit: int) -> bytes:
    if value is None:
        return _COORDINATE_NULL
    if not -limit <= value <= limit:
        raise ValueError("Coordenada de estação inválida")
    return _FLOAT64.pack(value)


def _decode_coordinate(encoded: bytes, limit: int) -> float | None:
    if encoded == _COORDINATE_NULL:
        return None
    value, = _FLOAT64.unpack(encoded)
    if not -limit <= value <= limit:
        raise FormatError("Coordenada de estação inválida")
    return value


def write_station(
    path: str | Path,
    station_id: int,
    code: str,
    latitude: float | None,
    longitude: float | None,
) -> None:
    """Write a geographically versionable station record."""
    _check_station_id(station_id)
    encoded = code.encode("utf-8")
    if not 1 <= len(encoded) <= 65535:
        raise ValueError("O código da estação deve ter entre 1 e 65535 bytes UTF-8")
    header = _STATION_HEADER.pack(
        _STATION_MAGIC,
        _VERSION,
        station_id,
        _encode_coordinate(latitude, 90),
        _encode_coordinate(longitude, 180),
        len(encoded),
    )
    with Path(path).open("wb") as stream:
        stream.write(header)
        stream.write(encoded)


def read_station(path: str | Path) -> StationRecord:
    """Read the station identifier, source code, latitude and longitude."""
    with Path(path).open("rb") as stream:
        magic, version, station_id, lat, lon, length = _STATION_HEADER.unpack(
            _read_exact(stream, _STATION_HEADER.size)
        )
        if (magic, version) != (_STATION_MAGIC, _VERSION):
            raise FormatError("Versão ou assinatura inválida do cadastro binário")
        if station_id == 0 or length == 0:
            raise FormatError("Cabeçalho inválido do cadastro binário")
        raw_code = _read_exact(stream, length)
        if stream.read(1):
            raise FormatError("Dados após o fim do cadastro binário")
        try:
            code = raw_code.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FormatError("Código da estação inválido em UTF-8") from exc
        return station_id, code, _decode_coordinate(lat, 90), _decode_coordinate(lon, 180)


def _scaled_integer(value: Decimal, scale: int) -> int | None:
    """Try exact integral scaling without involving the active Decimal context."""
    if value.is_zero():
        return 0
    parts = value.as_tuple()
    assert isinstance(parts.exponent, int)
    integer_digits = len(parts.digits) + parts.exponent + scale
    if not 1 <= integer_digits <= 5 or any(parts.digits[integer_digits:]):
        return None
    scaled = 0
    for digit in parts.digits[:integer_digits]:
        scaled = scaled * 10 + digit
    scaled *= 10 ** max(0, integer_digits - len(parts.digits))
    return -scaled if parts.sign else scaled


def _encode_hour(values: ObservationValues) -> tuple[bytes, bytes | None]:
    temperature, humidity = values
    if temperature is None and humidity is None:
        return _NULL_ROW, None

    precise_temperature = b""
    precise_humidity = b""
    encoded_temperature = _TEMP_NULL
    encoded_humidity = _HUMIDITY_NULL
    if temperature is not None:
        if not temperature.is_finite():
            raise ValueError("Temperatura deve ser finita")
        scaled = _scaled_integer(temperature, 1)
        if scaled is not None and -32766 <= scaled <= 32767:
            encoded_temperature = scaled
        else:
            encoded_temperature = _TEMP_PRECISE
            precise_temperature = str(temperature).encode("ascii")
    if humidity is not None:
        if not humidity.is_finite() or not 0 <= humidity <= 100:
            raise ValueError("Umidade relativa deve estar entre 0 e 100")
        scaled = _scaled_integer(humidity, 0)
        if scaled is not None:
            encoded_humidity = scaled
        else:
            encoded_humidity = _HUMIDITY_PRECISE
            precise_humidity = str(humidity).encode("ascii")

    hour = _HOUR.pack(encoded_temperature, encoded_humidity)
    if precise_temperature or precise_humidity:
        return hour, precise_temperature + b"\0" + precise_humidity
    return hour, None


def write_observations(
    path: str | Path,
    station_id: int,
    year: int,
    values: Iterable[tuple[int, Decimal | None, Decimal | None]],
) -> None:
    """Write ordered, unique observation rows at their UTC-year hour offsets.

    Publication of the finished file is the responsibility of storage.
    """
    _check_station_id(station_id)
    count = hours_in_year(year)
    start = (date(year, 1, 1) - _EPOCH).days * 86400
    precise: list[tuple[int, bytes]] = []
    with Path(path).open("wb") as stream:
        stream.write(_OBSERVATION_HEADER.pack(
            _OBSERVATION_MAGIC, _VERSION, station_id, start, count, 0
        ))
        position = 0
        for index, temperature, humidity in values:
            if not position <= index < count:
                raise ValueError("As horas devem ser únicas, crescentes e pertencer ao ano")
            encoded, precision = _encode_hour((temperature, humidity))
            stream.write(_GAP * (index - position))
            stream.write(encoded)
            if precision is not None:
                precise.append((index, precision))
            position = index + 1
        stream.write(_GAP * (count - position))
        if precise:
            offset = stream.tell() + len(precise) * _PRECISE_INDEX.size
            for index, payload in precise:
                stream.write(_PRECISE_INDEX.pack(index, offset, len(payload)))
                offset += len(payload)
            for _, payload in precise:
                stream.write(payload)
            stream.seek(0)
            stream.write(_OBSERVATION_HEADER.pack(
                _OBSERVATION_MAGIC, _VERSION, station_id, start, count, len(precise)
            ))


def _decode_precise_number(payload: bytes) -> Decimal:
    try:
        number = Decimal(payload.decode("ascii"))
    except (InvalidOperation, UnicodeDecodeError) as exc:
        raise FormatError("Valor decimal inválido") from exc
    if not number.is_finite():
        raise FormatError("Valor decimal não finito")
    return number


class ObservationBlock:
    """Random and sequential access to one immutable UTC station-year file."""

    def __init__(self, path: str | Path) -> None:
        self._stream = Path(path).open("rb")
        try:
            self._read_header()
        except BaseException:
            self._stream.close()
            raise

    def _read_header(self) -> None:
        magic, version, self.station_id, self.start_timestamp, self.count, self._precise_count = (
            _OBSERVATION_HEADER.unpack(_read_exact(self._stream, _OBSERVATION_HEADER.size))
        )
        if (magic, version) != (_OBSERVATION_MAGIC, _VERSION):
            raise FormatError("Versão ou assinatura inválida do bloco horário")
        if self.station_id == 0 or self.start_timestamp % 86400:
            raise FormatError("Cabeçalho inválido do bloco horário")
        try:
            start = _EPOCH + timedelta(days=self.start_timestamp // 86400)
        except OverflowError as exc:
            raise FormatError("Referência temporal inválida do bloco horário") from exc
        self.year = start.year
        if (
            (start.month, start.day) != (1, 1)
            or self.count != hours_in_year(self.year)
            or self._precise_count > self.count
        ):
            raise FormatError("Extensão temporal ou índice inválido do bloco horário")

        self._index_offset = _OBSERVATION_HEADER.size + self.count * _HOUR.size
        self._payload_offset = self._index_offset + self._precise_count * _PRECISE_INDEX.size
        self._size = os.fstat(self._stream.fileno()).st_size
        if self._precise_count == 0 and self._size != self._payload_offset:
            raise FormatError("Tamanho incompatível com o bloco horário")
        if self._size < self._payload_offset + self._precise_count * 2:
            raise FormatError("Seção de precisão truncada")

    def __enter__(self) -> ObservationBlock:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._stream.close()

    def _precise_at(self, index: int) -> tuple[bytes, bytes]:
        lower, upper = 0, self._precise_count
        while lower < upper:
            middle = (lower + upper) // 2
            self._stream.seek(self._index_offset + middle * _PRECISE_INDEX.size)
            found, offset, length = _PRECISE_INDEX.unpack(
                _read_exact(self._stream, _PRECISE_INDEX.size)
            )
            if found < index:
                lower = middle + 1
            elif found > index:
                upper = middle
            else:
                if (
                    offset < self._payload_offset
                    or length < 2
                    or offset + length > self._size
                ):
                    raise FormatError("Referência inválida para valor de precisão estendida")
                self._stream.seek(offset)
                parts = _read_exact(self._stream, length).split(b"\0")
                if len(parts) != 2:
                    raise FormatError("Registro de precisão estendida inválido")
                return parts[0], parts[1]
        raise FormatError("Valor de precisão estendida ausente no índice")

    def _decode_hour(self, index: int, temperature: int, humidity: int) -> ObservationSlot:
        if temperature == _TEMP_NULL and humidity == _HUMIDITY_NULL:
            return None
        if temperature == _TEMP_NULL and humidity == _HUMIDITY_NULL_ROW:
            return None, None

        precise_temperature = b""
        precise_humidity = b""
        if temperature == _TEMP_PRECISE or humidity == _HUMIDITY_PRECISE:
            precise_temperature, precise_humidity = self._precise_at(index)
            if (
                bool(precise_temperature) != (temperature == _TEMP_PRECISE)
                or bool(precise_humidity) != (humidity == _HUMIDITY_PRECISE)
            ):
                raise FormatError("Índice de precisão incompatível com o registro horário")

        decoded_temperature = None
        if temperature == _TEMP_PRECISE:
            decoded_temperature = _decode_precise_number(precise_temperature)
        elif temperature != _TEMP_NULL:
            decoded_temperature = Decimal(f"{temperature}e-1")

        decoded_humidity = None
        if humidity == _HUMIDITY_PRECISE:
            decoded_humidity = _decode_precise_number(precise_humidity)
            if not 0 <= decoded_humidity <= 100:
                raise FormatError("Umidade relativa inválida")
        elif 0 <= humidity <= 100:
            decoded_humidity = Decimal(humidity)
        elif humidity != _HUMIDITY_NULL:
            raise FormatError("Código inválido de umidade relativa")
        return decoded_temperature, decoded_humidity

    def at(self, index: int) -> ObservationSlot:
        """Read a single slot. None denotes an hour without a source row."""
        if not 0 <= index < self.count:
            raise IndexError("Hora fora do bloco")
        self._stream.seek(_OBSERVATION_HEADER.size + index * _HOUR.size)
        values = _HOUR.unpack(_read_exact(self._stream, _HOUR.size))
        return self._decode_hour(index, *values)

    def range(self, start: int, stop: int) -> Iterator[ObservationSlot]:
        """Yield the slots in the half-open [start, stop) interval."""
        if not 0 <= start <= stop <= self.count:
            raise IndexError("Intervalo fora do bloco")
        for first in range(start, stop, _READ_CHUNK):
            self._stream.seek(_OBSERVATION_HEADER.size + first * _HOUR.size)
            count = min(_READ_CHUNK, stop - first)
            data = _read_exact(self._stream, count * _HOUR.size)
            for index, values in enumerate(_HOUR.iter_unpack(data), first):
                yield self._decode_hour(index, *values)
