import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

from shadow_versions import scan_versions


class SnapshotTests(unittest.TestCase):
    def test_same_path_across_snapshots_and_folder_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            roots = {}
            rows = []
            for index, year in enumerate(("2023", "2024")):
                root = Path(temp) / year
                (root / "Pictures").mkdir(parents=True)
                (root / "Pictures/trip.jpg").write_bytes(year.encode())
                (root / "different.jpg").touch()
                device = rf"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy{index}"
                roots[device + "\\"] = root
                rows.append(dict(device=device, date=year))
            job = dict(status="running", matches=[])
            scope = PureWindowsPath("C:/Pictures")
            with patch("shadow_versions.snapshots", return_value=rows), patch("shadow_versions.Path", side_effect=lambda p: roots.get(p) or Path(p)):
                scan_versions(scope, job)
            self.assertEqual([(p.read_bytes(), original, date) for p, original, _, date in job["matches"]],
                             [(year.encode(), scope / "trip.jpg", year) for year in ("2023", "2024")])
