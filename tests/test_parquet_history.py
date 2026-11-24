import io
import re
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from http.client import RemoteDisconnected
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError

from nuvem_inmet.collectors import parquet_history as mirror
from nuvem_inmet.models import Observation, Station

try:
    import pyarrow
    import pyarrow.parquet
except ImportError:
    pyarrow = None


class _Response(io.BytesIO):
    def __init__(self, request, data, size, etag, start, stop, *, status=206):
        super().__init__(data)
        self.status = status
        self.headers = {"Content-Range": f"bytes {start}-{stop}/{size}", "ETag": etag}
        self._url = request.full_url

    def geturl(self):
        return self._url


def _mock_server(data, etag):
    def open_range(request, *, timeout):
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", request.get_header("Range"))
        first, last = (int(value) for value in match.groups())
        return _Response(request, data[first:last + 1], len(data), etag, first, last)

    return open_range


class RangeFileTests(unittest.TestCase):
    def test_prefetched_adjacent_ranges_obey_budget_and_are_reused(self):
        data = bytes(range(80))
        item = mirror._Object("test.parquet", len(data), '"test"')
        transfer = mirror._Transfer(12)
        stream = mirror._RangeFile(item, transfer)
        with patch.object(mirror, "urlopen", side_effect=_mock_server(data, item.etag)) as network:
            with ThreadPoolExecutor(max_workers=2) as workers:
                stream.prefetch([(10, 14), (14, 17), (54, 59)], workers)
            stream.seek(12)
            self.assertEqual(stream.read(5), data[12:17])
            stream.seek(54)
            self.assertEqual(stream.read(5), data[54:59])
            stream.seek(0)
            with self.assertRaisesRegex(ValueError, "Limite de transferência"):
                stream.read(1)
            self.assertEqual(network.call_count, 2)
            self.assertEqual(transfer.used, 12)

    def test_refuses_a_response_from_an_unexpected_version(self):
        item = mirror._Object("test.parquet", 6, '"expected"')
        reader = mirror._RangeFile(item, mirror._Transfer(10))
        with patch.object(mirror, "urlopen", side_effect=_mock_server(b"sample", '"different"')):
            with self.assertRaisesRegex(ValueError, "versão"):
                reader.read(3)

    def test_refreshes_an_expired_download_url(self):
        item = mirror._Object("test.parquet", 6, '"expected"')
        transfer = mirror._Transfer(6)
        reader = mirror._RangeFile(item, transfer)
        expired = "https://storage.googleapis.com/expired"
        fresh = "https://storage.googleapis.com/fresh"
        reader._url = expired

        def serve(request, *, timeout):
            if request.full_url == expired:
                raise HTTPError(expired, 403, "expired", {}, None)
            response = _mock_server(b"sample", item.etag)(request, timeout=timeout)
            response._url = fresh
            return response

        with patch.object(mirror, "urlopen", side_effect=serve) as network:
            self.assertEqual(reader.read(3), b"sam")
            self.assertEqual(reader.read(3), b"ple")
            self.assertEqual([call.args[0].full_url for call in network.call_args_list], [expired, item.url, fresh])
        self.assertEqual(transfer.used, 6)

    def test_retries_transient_range_error_without_double_budget_charge(self):
        item = mirror._Object("test.parquet", 6, '"expected"')
        transfer = mirror._Transfer(3)
        reader = mirror._RangeFile(item, transfer)
        serve = _mock_server(b"sample", item.etag)
        attempts = 0

        def intermittent(request, *, timeout):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise HTTPError(request.full_url, 503, "transient", {}, None)
            if attempts == 2:
                raise RemoteDisconnected("transient")
            return serve(request, timeout=timeout)

        with patch.object(mirror, "urlopen", side_effect=intermittent):
            with patch.object(mirror, "sleep") as wait:
                self.assertEqual(reader.read(3), b"sam")
                self.assertEqual([call.args for call in wait.call_args_list], [(1,), (3,)])
        self.assertEqual(transfer.used, 3)

    def test_reuses_valid_projected_chunk_across_processes_and_replaces_corruption(self):
        item = mirror._Object("test.parquet", 6, '"expected"')
        serve = _mock_server(b"sample", item.etag)
        with TemporaryDirectory() as directory:
            cache = Path(directory)
            with patch.object(mirror, "urlopen", side_effect=serve) as network:
                first = mirror._RangeFile(item, mirror._Transfer(3), cache)
                self.assertEqual(first.read(3), b"sam")
                second = mirror._RangeFile(item, mirror._Transfer(3), cache)
                self.assertEqual(second.read(3), b"sam")
                self.assertEqual(network.call_count, 1)
                (cache / "0-3.range").write_bytes(b"corrupt")
                second.seek(0)
                self.assertEqual(second.read(3), b"sam")
                self.assertEqual(network.call_count, 2)


@unittest.skipIf(pyarrow is None, "instale o extra [parquet]")
class ParquetHistoryTests(unittest.TestCase):
    def test_projection_and_utc_boundaries(self):
        start = datetime(2024, 2, 29, tzinfo=UTC)
        end = datetime(2024, 3, 1, tzinfo=UTC)
        before = datetime(2019, 12, 31, 23)
        table = pyarrow.table({
            "station_code": ["A807", "A807", "83222", "83222"],
            "latitude": [-25.45, -25.45, -12.66, -12.67],
            "longitude": [-49.23, -49.23, -39.08, -39.08],
            "datetime": [before, start.replace(tzinfo=None), start.replace(tzinfo=None), end.replace(tzinfo=None)],
            "dry_bulb_temp_C": [14.5, 22.9, 20.125, 26.2],
            "relative_humidity_pct": [95.0, 94.0, None, 72.0],
            "not_selected": ["sample", "sample", "sample", "sample"],
        })
        buffer = pyarrow.BufferOutputStream()
        pyarrow.parquet.write_table(table, buffer, row_group_size=2)
        sample = buffer.getvalue().to_pybytes()
        item = mirror._Object("test.parquet", len(sample), '"test"')
        history = mirror.ParquetHistory(start, end)
        with patch.object(mirror, "_OBJECTS", (item,)):
            with patch.object(mirror, "urlopen", side_effect=_mock_server(sample, item.etag)):
                self.assertEqual(list(history.stations()), [
                    Station("83222", None, None), Station("A807", -25.45, -49.23),
                ])
                self.assertEqual(list(history.observations()), [
                    Observation("A807", start, Decimal("22.9"), Decimal("94")),
                    Observation("83222", start, Decimal("20.125"), None),
                ])
                self.assertEqual(list(history.observations(["83222"])), [
                    Observation("83222", start, Decimal("20.125"), None),
                ])
        self.assertGreater(history.bytes_transferred, 0)

    def test_rejects_window_outside_pinned_coverage(self):
        with self.assertRaisesRegex(ValueError, "2026-01-01 exclusivo"):
            mirror.ParquetHistory(datetime(2025, 12, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC))


if __name__ == "__main__":
    unittest.main()
