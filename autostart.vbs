' Hidden wrapper for check-and-start.bat, used by the "local-web-mcp-stack"
' scheduled task at logon. Window style 0 = hidden; False = don't wait.
CreateObject("WScript.Shell").Run """C:\Users\Tai\local-web-mcp\check-and-start.bat""", 0, False
