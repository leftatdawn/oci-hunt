"""Retry-launch an Oracle Cloud (OCI) instance until capacity is available.

All settings come from environment variables (GitHub Secrets), nothing is
hard-coded. Exits 0 on success or when the time budget runs out, 1 on a
config/permission error that retrying cannot fix.
"""
import os
import random
import sys
import time

import oci
import requests

# Stop a bit before GitHub's 6h job limit so the run ends cleanly.
TIME_BUDGET_SECONDS = 5.5 * 3600


def env(key, default=None):
    value = os.environ.get(key, default)
    if value is None or value == "":
        sys.exit(f"Missing required env var: {key}")
    return value


def notify(text):
    """Send a Telegram message. Silent no-op if not configured."""
    token = os.environ.get("TG_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not (token and chat_id):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=15,
        )
    except Exception:
        # Never print the exception: the URL contains the bot token.
        print("Telegram notify failed", flush=True)


def wib(ts=None):
    """Format a timestamp as HH:MM:SS in WIB (UTC+7)."""
    ts = time.time() if ts is None else ts
    return time.strftime("%H:%M:%S", time.gmtime(ts + 7 * 3600))


def log(text):
    """Print a line prefixed with the current WIB time."""
    print(f"{wib()} WIB | {text}", flush=True)


def mark_success():
    """Tell the workflow it can disable itself."""
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write("success=true\n")


config = {
    "user": env("OCI_USER"),
    "tenancy": env("OCI_TENANCY"),
    "fingerprint": env("OCI_FINGERPRINT"),
    "region": env("OCI_REGION"),
    "key_content": env("OCI_PRIVATE_KEY"),
}
oci.config.validate_config(config)
compute = oci.core.ComputeClient(config)

compartment = env("COMPARTMENT_ID")
name = os.environ.get("INSTANCE_NAME", "arm-free")

shape = os.environ.get("SHAPE", "VM.Standard.A1.Flex")
ocpus = float(os.environ.get("OCPUS", "2"))
memory_gb = float(os.environ.get("MEMORY_GB", "12"))

# Safety guard: per Oracle's docs (checked Oct 2026) Always Free A1 = 2 OCPU /
# 12 GB total. Anything above that is billed on Pay As You Go accounts.
# Set ALLOW_OVER_FREE=1 only if you really know what you are doing.
if shape == "VM.Standard.A1.Flex" and (ocpus > 2 or memory_gb > 12) \
        and os.environ.get("ALLOW_OVER_FREE") != "1":
    sys.exit("Refusing to launch: above the Always Free A1 limit (2 OCPU / 12 GB).")

details = oci.core.models.LaunchInstanceDetails(
    availability_domain=env("AVAILABILITY_DOMAIN"),
    compartment_id=compartment,
    display_name=name,
    shape=shape,
    shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
        ocpus=ocpus,
        memory_in_gbs=memory_gb,
    ),
    source_details=oci.core.models.InstanceSourceViaImageDetails(
        image_id=env("IMAGE_ID")
    ),
    create_vnic_details=oci.core.models.CreateVnicDetails(
        subnet_id=env("SUBNET_ID"), assign_public_ip=True
    ),
    metadata={"ssh_authorized_keys": env("SSH_PUBLIC_KEY")},
)
if "Flex" not in shape:
    # Fixed shapes (e.g. VM.Standard.E2.1.Micro) don't accept a shape config.
    details.shape_config = None


def already_exists():
    items = oci.pagination.list_call_get_all_results(
        compute.list_instances, compartment, display_name=name
    ).data
    return [i for i in items if i.lifecycle_state not in ("TERMINATING", "TERMINATED")]


def main():
    notify("OCI hunt: run dimulai, mulai nyari slot.")

    if already_exists():
        notify("OCI hunt: instance sudah ada, berhenti.")
        mark_success()
        return 0

    deadline = time.time() + TIME_BUDGET_SECONDS
    attempt = 0
    log(f"Mulai. Batas sesi {TIME_BUDGET_SECONDS / 3600:.1f} jam.")

    while time.time() < deadline:
        attempt += 1
        extra = 0
        reason = "capacity penuh / error sementara"
        log(f"[{attempt}] mencoba membuat instance...")
        try:
            instance = compute.launch_instance(details).data
            notify(f"OCI hunt: BERHASIL! Instance {instance.display_name} dibuat "
                   f"(state: {instance.lifecycle_state}). Cek Console Oracle.")
            log(f"[{attempt}] BERHASIL, instance dibuat.")
            mark_success()
            return 0
        except oci.exceptions.ServiceError as e:
            # "Out of host capacity" comes back as HTTP 500 / InternalError.
            log(f"[{attempt}] gagal: {e.status} {e.code}")
            retryable = e.status in (429, 500, 502, 503, 504) or \
                "capacity" in (e.message or "").lower()
            if not retryable:
                notify(f"OCI hunt: error {e.status} {e.code}. Cek konfigurasi, "
                       f"retry tidak akan membantu.")
                return 1
            if e.status == 429:
                extra = 120  # rate limited: back off harder
                reason = "kena rate limit 429"
        except Exception as e:
            # Network hiccup: the launch may have gone through, so check.
            log(f"[{attempt}] gagal: {type(e).__name__}")
            reason = "error jaringan"
            try:
                if already_exists():
                    notify("OCI hunt: instance terdeteksi sudah ada, berhenti.")
                    mark_success()
                    return 0
            except Exception:
                pass

        delay = extra + random.randint(45, 90)
        sisa = max(0, int(deadline - time.time()))
        next_at = wib(time.time() + delay)
        log(f"[{attempt}] tunggu {delay} detik ({reason}); percobaan ke-{attempt + 1} "
            f"jam {next_at} WIB; sisa sesi {sisa // 60} menit")
        time.sleep(delay)

    notify(f"OCI hunt: sesi habis setelah {attempt} percobaan, run berikutnya lanjut.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
