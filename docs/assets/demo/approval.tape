# Records the README's one-minute approval demo as docs/assets/demo/approval.gif: install from
# PyPI, then run the approval quickstart. The install goes into a fresh virtual environment in a
# temporary directory, made with $PYTHON (default python3, which must be 3.12 or later).
# Needs vhs (https://github.com/charmbracelet/vhs). From the repository root:
#   vhs docs/assets/demo/approval.tape

Output docs/assets/demo/approval.gif

Set Shell bash
Set FontSize 15
Set Width 1100
Set Height 680
Set Padding 24
Set Theme "Builtin Dark"
Set TypingSpeed 45ms
Set WaitPattern /\$ ?$/
Set WaitTimeout 180s

Hide
Type 'cd "$(mktemp -d)" && "${PYTHON:-python3}" -m venv venv && source venv/bin/activate'
Enter
Type 'python -m pip install --quiet --upgrade pip'
Enter
Type 'export PIP_DISABLE_PIP_VERSION_CHECK=1 PS1="$ " && clear'
Enter
Wait
Show

Type "python -m pip install --quiet --pre algen-agent-runtime"
Sleep 500ms
Enter
Wait
Sleep 1s

Type "python -m examples.quickstart_approval.app"
Sleep 500ms
Enter
Wait+Screen@20s /Approve or reject\?/
Sleep 1.5s
Type "approve"
Sleep 600ms
Enter
Wait+Screen@20s /"status": "applied"/
Sleep 4s
