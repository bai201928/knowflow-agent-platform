"""Condensed GitHub runner entrypoint for the V2 teaching reference.

The complete downloadable Demo has a multi-file scenario registry. This small repository version
keeps the same CLI idea while avoiding copying every educational adapter into the specification repo.
"""

from knowflow_v2_demo import list_scenarios, render_scenario


def main() -> None:
    """Print available scenarios and a selected V2 execution-plan trace."""

    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", nargs="?")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list or not args.scenario:
        for name in list_scenarios():
            print(name)
        return
    print(render_scenario(args.scenario))


if __name__ == "__main__":
    main()
