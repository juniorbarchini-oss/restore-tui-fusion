# Contributing

Issues and pull requests are welcome. This project can write to real disks, so changes are
reviewed carefully.

## Workflow

- `main` holds releases and changes only through a PR from `develop`. Branch `feature/*` or `fix/*` from
  `develop` and open the PR against `develop`.
- Semantic commit messages: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`.
- Run the formatter (`black`, line length 100) and the tests (`./run-tests.sh`) before pushing.
- Tests that need real disks, root, SSH or btrfs must be marked and skipped by default; do not claim
  something works until it has been run on real hardware (see `docs/HARDWARE_CHECKLIST.md`).

## Rules for the code

- Respect the safety rules in the README. They are not negotiable.
- UI text, menus and prompts are in English. Code comments in Spanish or English are both fine.
- Never commit secrets, real host names, real IPs, user names or personal paths, in code, tests, docs,
  issues or pull requests. Use placeholders such as `backup-host.example` and `<user>`.

## License

By contributing you agree that your contribution is licensed under the MIT License (see `LICENSE`).
