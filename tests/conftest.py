import csv
import json

import pytest

from apex_diligence.pipeline import RunInputs, run
from apex_diligence.synthetic import COMPANY, generate


@pytest.fixture(scope="session")
def demo_files(tmp_path_factory):
    """The synthetic company's files, generated once for the whole test run."""
    return generate(tmp_path_factory.mktemp("data"))


@pytest.fixture(scope="session")
def planted(demo_files):
    return json.loads(demo_files["planted"].read_text())


@pytest.fixture(scope="session")
def demo_inputs(demo_files):
    return RunInputs(gl=demo_files["gl"], bank=demo_files["bank"], mapping=demo_files["mapping"],
                     rules=demo_files["rules"], company=COMPANY)


@pytest.fixture(scope="session")
def demo_result(demo_inputs):
    return run(demo_inputs)


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
    return path
