"""
Copyright (C) 2026 - PsychedelicPalimpsest
Feel free to share this within the bounds of 
CC0 1.0 Universal
"""


import json
import subprocess
import os, sys

import requests

from os.path import dirname, abspath, exists, join, splitext
from tempfile import mktemp

from subprocess import DEVNULL, check_call

# Import Gryla tooling
SCRIPTS_DIR = join(dirname(dirname(abspath(__file__))), "utils", "scripts")
sys.path.append(SCRIPTS_DIR)

from mcjar import download_cached, get_piston_file, get_storage_dir, download_cached
from jar_marker import taint_jar, generate_tiny


JAVA = "/usr/lib/jvm/java-8-openjdk/jre/bin/java"


def get_build_data_path() -> str:
    data_path = join(get_storage_dir(), "spigot_build_data")
    inner_path = join(data_path, "BuildData")

    if not exists(inner_path):
        os.makedirs(data_path)

        check_call(
            [
                "git",
                "clone",
                "https://hub.spigotmc.org/stash/scm/spigot/builddata.git",
                inner_path,
            ]
        )
    return inner_path


SPECIAL_SOURCE = join(get_build_data_path(), "bin", "SpecialSource-2.jar")


def set_build_data(commit: str):
    data = get_build_data_path()
    check_call(
        ["git", "checkout", commit],
        cwd=data,
        stdout=DEVNULL,
        stderr=DEVNULL,
    )


def map_jar(
    input_jar, input_mappings, exclude: None | str = None, auto_lvt: bool = False
):
    output = mktemp(".jar")

    check_call(
        [JAVA, "-jar", SPECIAL_SOURCE, "map"]
        + (["--auto-lvt", "BASIC"] if auto_lvt else [])
        + (["-e", exclude] if exclude is not None else [])
        + ["-i", input_jar, "-m", input_mappings, "-o", output],
    )

    return output


def get_versions() -> list[tuple[str, str]]:
    lines = requests.get("https://hub.spigotmc.org/versions/").text.splitlines()

    files = [
        # Ex: <a href="1.10.2.json">1.10.2.json</a>
        line.split('"')[1]
        for line in lines
        if line.startswith("<a ")
    ]

    return [
        (version, "https://hub.spigotmc.org/versions/" + version) for version in files
    ]


# This function is EVIL. Theoretically this should be run in a container, but I don't really care
def run_map_command(data_dir, cmd, *args):
    assert cmd.startswith("java -jar BuildData/bin/SpecialSource-2.jar")

    cmd = cmd.strip().replace("  ", " ").split(" ")
    cmd = [seg if not seg[0] == "{" else args[int(seg[1])] for seg in cmd]

    cmd[0] = JAVA
    print("Running map command:", " ".join(cmd))

    subprocess.check_call(cmd, cwd=dirname(data_dir))


# Intelligently renames the fields and methods.
def tiny_renamer(tiny_file, ignore_conflicts: bool = False):
    print(f"[*] Starting conflict resolution on {tiny_file}...")

    entries = []
    # Key: (ObfName, Descriptor) -> Set of Candidate Names
    sig_candidates = {}

    # --- Phase 1: Parse and Collect Candidates ---
    with open(tiny_file, "r") as fin:
        for line in fin:
            clean_line = line.strip()
            if not clean_line:
                continue

            segs = clean_line.split("\t")
            entry = {"type": "OTHER", "segs": segs, "original": line}

            if clean_line.startswith("FIELD"):
                entry["type"] = "FIELD"

            elif clean_line.startswith("METHOD"):
                entry["type"] = "METHOD"
                if len(segs) >= 5:
                    obf, desc, mapped = segs[3], segs[2], segs[4]
                    sig_key = (obf, desc)
                    if sig_key not in sig_candidates:
                        sig_candidates[sig_key] = set()
                    sig_candidates[sig_key].add(mapped)

            entries.append(entry)

    # --- Phase 2: Unify Source Hierarchies ---
    print("[*] Unifying source hierarchies...")
    source_winners = {}  # (Obf, Desc) -> BestName

    for sig, candidates in sig_candidates.items():
        if not candidates:
            continue

        human_names = [n for n in candidates if not n.startswith("func_")]
        if human_names:
            winner = sorted(human_names)[0]
        else:
            winner = sorted(list(candidates))[0]

        source_winners[sig] = winner

    # --- Phase 3: Unify Target Names ---
    print("[*] Checking for target collisions...")

    # Map (TargetName, Descriptor) -> List[ObfName]
    target_claims = {}

    for (obf, desc), best_name in source_winners.items():
        target_key = (best_name, desc)
        if target_key not in target_claims:
            target_claims[target_key] = []
        target_claims[target_key].append(obf)

    final_sig_map = {}

    KNOWN_JAR_CONFLICTS = {
        ("format", "(I)Ljava/lang/String;"),
    }

    for (t_name, desc), obf_list in target_claims.items():
        is_jar_conflict = (t_name, desc) in KNOWN_JAR_CONFLICTS

        if len(obf_list) > 1 or is_jar_conflict:
            obf_list.sort()

            if is_jar_conflict:
                print(
                    f"[WARN] JAR Conflict on {t_name}{desc}: "
                    f"claimed by {obf_list}. Renaming all."
                )
                start_idx = 0
            else:
                winner = obf_list[0]
                print(
                    f"[WARN] Target Collision on {t_name}{desc}: "
                    f"claimed by {obf_list}"
                )
                final_sig_map[(winner, desc)] = t_name
                start_idx = 1

            for loser in obf_list[start_idx:]:
                new_name = f"{t_name}_{loser}"
                final_sig_map[(loser, desc)] = new_name
                print(f"       -> Renaming {loser} to {new_name}")
        else:
            final_sig_map[(obf_list[0], desc)] = t_name

    # --- Phase 4: Apply & Write ---
    final_lines = []
    lines_to_check = []

    print("[*] Applying resolved mappings...")

    for entry in entries:
        if entry["type"] == "OTHER":
            final_lines.append(entry["original"])
            continue

        segs = entry["segs"]

        if entry["type"] == "METHOD":
            obf, desc = segs[3], segs[2]

            if (obf, desc) in final_sig_map:
                segs[4] = final_sig_map[(obf, desc)]

            if ignore_conflicts:
                entry["dedup_key"] = f"{segs[1]}@{desc}:{segs[4]}"
                lines_to_check.append(entry)
            else:
                final_lines.append("\t".join(segs) + "\n")

        elif entry["type"] == "FIELD":
            if ignore_conflicts:
                entry["dedup_key"] = f"{segs[1]}@{segs[4]}"
                lines_to_check.append(entry)
            else:
                final_lines.append("\t".join(segs) + "\n")

    # --- Phase 5: Local Class Collision Repair ---
    if ignore_conflicts:
        seen_keys = set()

        for entry in lines_to_check:
            k = entry["dedup_key"]
            if k in seen_keys:
                entry["is_dupe"] = True
            else:
                seen_keys.add(k)

        for entry in lines_to_check:
            segs = entry["segs"]
            if entry.get("is_dupe"):
                segs[4] = f"{segs[4]}_{segs[3]}"
            final_lines.append("\t".join(segs) + "\n")

    with open(tiny_file, "w") as fout:
        fout.writelines(final_lines)

    print("[*] Finished renaming operations")


def spigot_map_jar(build_dir, info_json, input_jar) -> str:
    # TODO: ADD TEST FOR MOJMAP MAPPINGS

    # Until 1.13.2, you must map yourself
    if 84 > info_json.get("toolsVersion", 0):
        class_mapped = map_jar(
            input_jar, join(build_dir, "mappings", info_json["classMappings"])
        )
        member_mapped = map_jar(
            class_mapped, join(build_dir, "mappings", info_json["memberMappings"])
        )
        os.remove(class_mapped)
        return member_mapped
    else:
        class_mapped = mktemp(".jar")
        run_map_command(
            build_dir,
            info_json["classMapCommand"],
            input_jar,
            join(build_dir, "mappings", info_json["classMappings"]),
            class_mapped,
        )

        member_mapped = mktemp(".jar")
        run_map_command(
            build_dir,
            info_json["memberMapCommand"],
            class_mapped,
            join(build_dir, "mappings", info_json["memberMappings"]),
            member_mapped,
        )
        os.remove(class_mapped)
        return member_mapped


def spigot_generate_tiny(version_file, url, use_renamer=False):
    out_path = join("tiny_v1s", splitext(version_file)[0] + ".tiny")

    if exists(out_path):
        return out_path

    version_json = requests.get(url).json()

    # Commit hash for where the mappings can be found
    build_data_commit = version_json["refs"]["BuildData"]

    build_data_path = get_build_data_path()
    set_build_data(build_data_commit)

    with open(join(build_data_path, "info.json")) as f:
        info_json = json.load(f)

    if "serverUrl" in info_json:
        server_jar = download_cached(info_json["serverUrl"], "server.jar")
    else:
        server_jar = get_piston_file(info_json["minecraftVersion"], "server")

    tainted_jar = mktemp(".jar")
    taint_jar(server_jar, tainted_jar)

    mapped_jar = spigot_map_jar(build_data_path, info_json, tainted_jar)

    generate_tiny(mapped_jar, out_path, remove_identical_members=True)
    if use_renamer:
        tiny_renamer(out_path)

    os.remove(mapped_jar)
    os.remove(tainted_jar)


def version_dot_to_int(dot_ver):
    val = 100**3
    dots = dot_ver.split(".")

    ret = 0
    while dots:
        ret += int(dots.pop(0)) * val
        val //= 100
    return ret


if __name__ == "__main__":
    versions = []
    for version_file, url in get_versions():
        version, _ = splitext(version_file)

        if not "." in version:
            continue
        if "-" in version:
            continue

        versions.append((version_dot_to_int(version), version_file, url))

    versions = sorted(versions, key=lambda version, *_: version)
    for version_int, version_file, url in versions:
        if version_int > version_dot_to_int("1.16.5"):
            break

        print(version_file)
        spigot_generate_tiny(
            version_file, url, use_renamer=version_int >= version_dot_to_int("1.9")
        )
