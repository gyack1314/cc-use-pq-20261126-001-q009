"""提供离线捕获、续传与恢复验证命令。"""

import argparse
import json
from pathlib import Path

from strata.field.check import check_parcel
from strata.field.parcel import DEFAULT_PART, build_parcel, open_parcel
from strata.field.receive import verify_package
from strata.field.uplink import Client, Uplink
from strata.runtime.errors import VaultError


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)

    build = commands.add_parser("capture", help="Capture the bytes of a local field directory")
    build.add_argument("source", type=Path)
    build.add_argument("parcel", type=Path)
    build.add_argument("--collection", required=True)
    build.add_argument("--token", required=True)
    build.add_argument("--parent", required=True, type=int)
    build.add_argument("--lifetime", type=int, default=7 * 86400)
    build.add_argument("--part-size", type=int, default=DEFAULT_PART)

    inspect = commands.add_parser("inspect", help="Read a parcel identity and the logical contents")
    inspect.add_argument("parcel", type=Path)

    check = commands.add_parser("check", help="Verify a field parcel before transit")
    check.add_argument("parcel", type=Path)
    check.add_argument("--limit", type=int, default=50)

    submit = commands.add_parser("submit", help="Send missing parts and optionally close the deposit")
    submit.add_argument("parcel", type=Path)
    submit.add_argument("--vault-url", required=True)
    submit.add_argument("--maximum-parts", type=int)
    submit.add_argument("--keep-open", action="store_true")

    receive = commands.add_parser("verify", help="Check a checkout against a known archival reference")
    receive.add_argument("package", type=Path)
    receive.add_argument("--collection", required=True)
    receive.add_argument("--revision", required=True, type=int)
    receive.add_argument("--manifest-sha256", required=True)
    receive.add_argument("--extract", type=Path)
    return cli


def execute(options) -> dict:
    if options.command == "capture":
        parcel = build_parcel(
            options.source, options.parcel, collection=options.collection, token=options.token,
            parent=options.parent, lifetime=options.lifetime, part_size=options.part_size,
        )
        return parcel.descriptor()
    if options.command == "inspect":
        return open_parcel(options.parcel).descriptor()
    if options.command == "check":
        return check_parcel(open_parcel(options.parcel), options.limit).as_dict()
    if options.command == "submit":
        parcel = open_parcel(options.parcel)
        with Client(options.vault_url) as endpoint:
            return Uplink(endpoint).send(parcel, publish=not options.keep_open,
                                         maximum_parts=options.maximum_parts).as_dict()
    return verify_package(
        options.package, expected_manifest=options.manifest_sha256,
        expected_collection=options.collection, expected_revision=options.revision,
        destination=options.extract,
    ).as_dict()


def main() -> None:
    try:
        result = execute(parser().parse_args())
    except VaultError as error:
        print(json.dumps({"error": error.as_dict()}, ensure_ascii=False))
        raise SystemExit(2) from None
    except OSError as error:
        print(json.dumps({"error": {"code": "local_io", "message": str(error)}}, ensure_ascii=False))
        raise SystemExit(2) from None
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if result.get("valid") is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
