# Records the README's one-minute approval demo as docs/assets/demo/approval.gif.
# Needs vhs (https://github.com/charmbracelet/vhs) and algen-agent-runtime installed in the
# active environment. From the repository root: vhs docs/assets/demo/approval.tape

Output docs/assets/demo/approval.gif

Set Shell bash
Set FontSize 15
Set Width 1100
Set Height 680
Set Padding 24
Set Theme "Builtin Dark"
Set TypingSpeed 45ms

Hide
Type 'cd "$(mktemp -d)" && export PS1="$ " && clear'
Enter
Show

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
