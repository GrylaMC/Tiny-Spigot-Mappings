"""
Copyright (C) 2026 - PsychedelicPalimpsest
Feel free to share this within the bounds of 
CC0 1.0 Universal
"""

import os
import sys, subprocess 
from os.path import join, dirname, abspath, splitext, exists


from tempfile import mktemp 

# Import Gryla tooling
SCRIPTS_DIR = join(dirname(dirname(abspath(__file__))), "utils", "scripts")
sys.path.append(SCRIPTS_DIR)



from mcjar import get_piston_file, REMAPPER


def jar_map(jar_from, jar_to, mapping_file, from_name, to_name):
    resp = subprocess.Popen(["java", "-jar", REMAPPER,
                             jar_from, jar_to, mapping_file, from_name, to_name]) 

    return resp.wait()



if __name__ == "__main__":
    for file in os.listdir("tiny_v1s"):
        print("[*] Attempting to map", file)
        path = join("tiny_v1s", file)

        version, _ = splitext(file)
        server_jar = get_piston_file(version, "server")


        out_jar = mktemp(".jar")

        if 0 != jar_map(server_jar, out_jar, path, "official",	"named"):
            print("Could not map:", version)

        if exists(out_jar):
            os.remove(out_jar)



