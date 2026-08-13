"""Upload the §15 contract PDFs into the shared Google Drive folder.

The contracts are the only evidence that can establish *recurring* revenue, so
without them ARR is unprovable no matter how much cash reconciles. This puts the
same 14 documents Feature 3 is benchmarked against into a real Drive folder, so the
Document Collector Agent has to find, download and hash them over the actual API
rather than from a fixture.

A service account has no Drive of its own ("Service Accounts do not have storage
quota"), so the files are created inside a folder a real user shared with it, and
the storage counts against that user's quota. That is the whole reason the folder
share is a setup step rather than something this script can arrange for itself.

    python -m scripts.seed_drive [--folder "RevenueProof Contracts"] [--dry-run]

Idempotent by filename: a contract already in the folder is left alone, so a partial
upload can simply be re-run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx

from app.connectors.auth import access_token_for
from app.connectors.synthetic import contracts as synthetic
from app.models.enums import SourceSystem

DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"


async def find_folder(client: httpx.AsyncClient, name: str) -> dict | None:
    response = await client.get(
        f"{DRIVE}/files",
        params={
            "q": "mimeType='application/vnd.google-apps.folder' and trashed=false",
            "fields": "files(id,name,capabilities(canAddChildren))",
        },
    )
    response.raise_for_status()
    folders = response.json().get("files", [])
    for folder in folders:
        if folder["name"].strip().lower() == name.strip().lower():
            return folder
    return folders[0] if len(folders) == 1 else None


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", default="RevenueProof Contracts")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    token = await access_token_for(SourceSystem.GOOGLE_DRIVE)
    if not token:
        raise SystemExit("no Google credential configured")

    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=120, headers=headers) as client:
        folder = await find_folder(client, args.folder)
        if folder is None:
            raise SystemExit(
                f"no folder named {args.folder!r} is shared with this service account"
            )
        if not folder.get("capabilities", {}).get("canAddChildren"):
            raise SystemExit(
                f"folder {folder['name']!r} is shared read-only — Editor is needed to upload"
            )
        print(f"folder: {folder['name']} ({folder['id']})")

        existing = await client.get(
            f"{DRIVE}/files",
            params={
                "q": f"'{folder['id']}' in parents and trashed=false",
                "fields": "files(id,name)",
            },
        )
        existing.raise_for_status()
        present = {f["name"] for f in existing.json().get("files", [])}
        print(f"already present: {len(present)}")

        uploaded = skipped = 0
        for contract in synthetic.CONTRACTS:
            name = contract.file_name
            if name in present:
                skipped += 1
                continue
            marks = []
            if contract.is_scanned:
                marks.append("image-only, forces OCR")
            if contract.is_ambiguous:
                marks.append("ambiguous recurring/one-time")
            if contract.is_amendment:
                marks.append("amendment")
            note = f"  ({', '.join(marks)})" if marks else ""
            print(f"  uploading {name}{note}")
            if args.dry_run:
                continue

            pdf = synthetic.render_pdf(contract)
            response = await client.post(
                UPLOAD,
                files={
                    "metadata": (
                        None,
                        json.dumps({"name": name, "parents": [folder["id"]]}),
                        "application/json",
                    ),
                    "file": (name, pdf, "application/pdf"),
                },
            )
            if response.status_code >= 300:
                print(f"    failed: {response.status_code} {response.text[:200]}")
                continue
            uploaded += 1

        print(f"\nuploaded {uploaded}, skipped {skipped} already present")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
