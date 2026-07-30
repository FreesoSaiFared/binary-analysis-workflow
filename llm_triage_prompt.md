# Prompt Template for AI Triage of Suspicious Binaries

Copy and paste the prompt below into ChatGPT, Claude, Gemini, or any other LLM to evaluate the false positive status of the repacked installer. It includes the exact logs and tool outputs from our analysis.

***

```markdown
You are an expert Malware Analyst and Reverse Engineer. I need you to evaluate whether a suspicious installer (`Bnd.exe`, flagged 33/61 on VirusTotal as a Trojan/HackTool) contains a malicious trojan, or if it is a safe "false positive" crack tool. 

I have statically extracted the installer cabinet using `innoextract` and run several command-line triage tools. I will provide the tool outputs below.

Please analyze the evidence step-by-step and provide a detailed verdict following this logical structure:
1. **Provenance Analysis**: Assess the digital signatures of the core files. Do they match the legitimate vendor?
2. **DLL Side-Loading Context**: Analyze the presence of `winspool.drv`. Is it acting as a crack wrapper? Explain how DLL hijacking is being used here to bypass registration, and whether the DLL contains malicious actions.
3. **Behavioral Analysis (Capabilities)**: Review the Capa capability reports for the unsigned components. Check for critical malware features:
   - Does it perform C2 (Command & Control) communications?
   - Does it contain network sockets, DNS lookups, or download functions?
   - Does it install persistence (run keys, scheduled tasks, services)?
   - Does it exfiltrate credentials or capture keystrokes?
4. **AV Verdict Comparison**: Explain why automated heuristics on VirusTotal flagged these techniques (side-loading, memory patching) as "Trojan:Win32/Tedy!MTB" or similar generic trojan classes.
5. **Final Verdict**: Give a clear explanation of whether the file is safe to run.

---

### EVIDENTIARY DATA

#### 1. Detect It Easy (DIE) Scan of `Bnd.exe`
```
PE32
Linker: Turbo Linker(2.25)
Compiler: Borland Delphi(2)
Compiler: TASM32(5.x)
Installer: Inno Setup Module(5.5.0)
Overlay: Start offset 66048, Size 29930891 bytes, Entropy 7.999 (High compression/packed)
```

#### 2. Statically Extracted File List (`innoextract --list`)
```
- "app\BandicamPortable.exe" (NSIS Portable Launcher wrapper)
- "app\App\Bandicam\bdcam.exe" (Core Bandicam executable)
- "app\App\Bandicam\bdcam64.dll" (Core DLL)
- "app\App\Bandicam\winspool.drv" (Unsigned local DLL)
- "tmp\bcact.exe" (Unsigned Activator binary)
- "tmp\bcact.bat" (Contains command: echo bandicam@bandisoft.com // LRepacks | bcact.exe)
```

#### 3. Sigcheck Digital Signature Audits
**Audit A: Core Executable (`bdcam.exe`)**
```
Verified:       Signed
Signing date:   14:22 26/12/2025
Publisher:      Bandicam Company Corp.
Company:        Bandicam Company
Description:    Bandicam - bdcam.exe
Prod version:   8.3.0.2533
SHA-256:        C4D2CD927BBA83C639EE267A5BFBB0A7318151B2306255AA11E52E560E484125
```

**Audit B: Hijack DLL (`winspool.drv`)**
```
Verified:       Unsigned (Spoofed version resource)
Link date:      08:23 08/07/2024 (Compiled recently)
Company:        Microsoft Corporation (Fake Spoof)
Description:    Windows Spooler Driver (Fake Spoof)
Entropy:        5.658
SHA-256:        19B78EF90CD2EDEBA6F20DAE20388BEE456DEF192654E91F7F875A21F2125715
```

**Audit C: Activator Tool (`bcact.exe`)**
```
Verified:       Unsigned
Link date:      15:32 11/07/2018
SHA-256:        1D4F413D06E98AB977DA26809D16b4CAC3C17BF50242D0770C3344FF8A2CEB83
```

#### 4. Mandiant Capa Behavioral Scan of Hijack DLL (`winspool.drv`)
```
ATT&CK Tactics: 
- DISCOVERY (File and Directory Discovery T1083)
- EXECUTION (Shared Modules T1129)

Detected Capabilities:
- hash data using SHA1
- get common file path
- check if file exists
- read file on Windows
- terminate process
- set thread local storage value
- link function at runtime (runtime API resolution)

*Network Connection Capabilities*: NONE detected (No sockets, WinINet, or DNS APIs imported).
*Persistence Capabilities*: NONE detected.
```

#### 5. Mandiant Capa Behavioral Scan of Activator (`bcact.exe`)
```
ATT&CK Tactics:
- DEFENSE EVASION (Obfuscated Files or Information T1027)
- DISCOVERY (Registry T1012, System Info T1082, Location T1614)
- EXECUTION (CLI arguments T1059, Runtime linking T1129)

Detected Capabilities:
- Compiled with Borland Delphi
- Cryptography: MD5 hashing, Blowfish encryption, XOR encoding
- System: Query/Set registry values (under CurrentUser/LocalMachine)
- Evasion: Reference analysis tools strings (Anti-debugging checks for IDA/OllyDbg/x64dbg)
- File System: Reads and writes files on Windows

*Network Connection Capabilities*: NONE detected (No sockets, WinINet, or DNS APIs imported).
*Persistence Capabilities*: NONE detected.
```
---
Please evaluate this evidence and compile your analytical report.
```
