import hashlib
import struct
import unittest
from decimal import Decimal, localcontext
from pathlib import Path
from tempfile import TemporaryDirectory

from nuvem_inmet.codec import (
    FormatError,
    ObservationBlock,
    hours_in_year,
    read_station,
    sha256_file,
    write_observations,
    write_station,
)


class BinaryFormatTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "block.bin"

    def test_compact_bytes_and_distinct_missing_values(self):
        rounded = (Decimal("-23.7"), Decimal("0"))
        rows = [(0, *rounded), (2, None, None), (3, None, Decimal("0"))]
        write_observations(self.path, 17, 2023, rows)

        data = self.path.read_bytes()
        self.assertEqual(len(data), 25 + 3 * 8760)
        self.assertEqual(
            struct.unpack("<4sBIqII", data[:25]),
            (b"NIMO", 1, 17, 1672531200, 8760, 0),
        )
        self.assertEqual(data[25:28], struct.pack("<hB", -237, 0))
        with ObservationBlock(self.path) as block:
            self.assertEqual(block.at(0), rounded)
            expected = [rounded, None, (None, None), (None, Decimal(0)), None]
            self.assertEqual(list(block.range(0, 5)), expected)

    def test_leap_year_and_lossless_precision(self):
        leap_day = (31 + 28) * 24
        precise = (Decimal("-12.3456789123456789123456789"), Decimal("89.123"))
        tiny = (Decimal("1.2E-999999"), None)
        last_hour = hours_in_year(2024) - 1
        with localcontext() as context:
            context.prec = 1
            write_observations(self.path, 17, 2024, [
                (leap_day, *precise),
                (last_hour, *tiny),
            ])
            with ObservationBlock(self.path) as block:
                self.assertEqual((block.station_id, block.year, block.count), (17, 2024, 8784))
                self.assertEqual(block.at(last_hour), tiny)
                self.assertEqual(list(block.range(leap_day - 1, leap_day + 2)), [
                    None, precise, None,
                ])
                with self.assertRaises(IndexError):
                    block.at(last_hour + 1)

    def test_duplicate_hour_is_rejected(self):
        with self.assertRaises(ValueError):
            write_observations(self.path, 17, 2024, [(5, None, None)] * 2)

    def test_truncated_file_is_rejected(self):
        write_observations(self.path, 17, 2024, [])
        with self.path.open("r+b") as stream:
            stream.truncate(self.path.stat().st_size - 1)
        with self.assertRaises(FormatError):
            ObservationBlock(self.path)

    def test_station_round_trip_and_checksum(self):
        station = (42, "A807", -25.4538, -49.2335)
        write_station(self.path, *station)
        self.assertEqual(read_station(self.path), station)
        self.assertEqual(
            sha256_file(self.path), hashlib.sha256(self.path.read_bytes()).hexdigest()
        )


if __name__ == "__main__":
    unittest.main()
