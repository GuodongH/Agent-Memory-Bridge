# E11 clean-install receipt

This directory records one clean-install run. It is not a GitHub CI result, a Windows result, or permission to merge or release.

The container image was `python:3.12-slim`, digest `sha256:02108f5d322dd89f1c9e552442c25acb0543dfdbc455693a5599624f20d9155d`. Before the probe, the container installed Debian `git` because `project init` needs a Git checkout. It did not install Codex, OpenCode, Claude, Cursor, Gemini, Hermes, Cline, Antigravity, or Windsurf.

The installed artifact was the host-built wheel `agent_memory_bridge-0.35.0-py3-none-any.whl`, sha256 `68a0b315a50a723c36562d1c41664bae9adc19b3e8a3547a017d4278d32886bc`, mounted at `/dist` and installed with `python -m pip install`. The installed `paths.py` sha256 is `77b130e0e7c83d1a47fa92bb9c4735a346ebd09454aaad98cb875ef04db6d553`, matching the source file in this checkout.

`probe.py` is the script that ran inside the container. `receipt.json` is its machine-readable result. `run.log` is the container output, including apt, pip, and the final probe status.

The receipt status is PASS for this container only: help ran, `setup` and `config` exited 2, `project init --yes` and `project resolve` returned `bound` / `project:e11-clean`, stdio discovery listed the same 17 tools, and a second stdio process recalled the stored decision. The filesystem walk found none of the named harness binaries. It skipped `/proc`, `/sys`, `/dev`, and `/run`. The legacy `~/.codex/mem-bridge` directory was not created.
