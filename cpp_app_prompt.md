# C++ CLI Developer Prompt: Binary False Positive Triage Tool

Copy and paste the prompt below into ChatGPT, Claude, or any advanced LLM to generate the C++ source code for a lightweight Windows CLI tool that automates our static analysis triage pipeline.

***

```markdown
You are an expert system programmer specializing in Windows API, PE file parsing, and security tooling. 

Please write a complete, production-ready C++ CLI application for Windows (compiled via MSVC) that automates the static analysis triage of a target PE executable. The tool must run locally, require no external dependencies (aside from standard Windows APIs), and output a structured JSON report.

### SPECIFICATION

The CLI tool should take a target file path as an argument: `triage.exe <path_to_binary>`

It must perform the following evaluation steps programmatically:

#### Step 1: PE Header & Installer Signature Check
1. Open the file and verify the DOS header (`MZ` signature) and PE signature (`PE\0\0`).
2. Scan the binary's overlay and resource data for common installer magic strings:
   - Check for Inno Setup: `"Inno Setup Setup Data"` or `"Inno Setup Messages"`.
   - Check for NSIS: Look for the NSIS magic signature `0xEFBEADDE` at the start of the overlay.
3. Report the detected package type (Installer Wrapper vs. Plain PE).

#### Step 2: Digital Signature Verification
1. Use the Windows Security/Cryptography APIs (`WinVerifyTrust` with `WINTRUST_ACTION_GENERIC_VERIFY_V2`) to programmatically verify the file signature of:
   - The main target binary.
   - Any extracted components (see Step 3).
2. Retrieve and output the signature status (Verified Signed, Unsigned, Self-Signed) and the **Subject Name/Publisher** if signed.

#### Step 3: Extraction Automation (For Installers)
1. If the binary is identified as an Inno Setup installer, programmatically locate `innoextract.exe` (first in the local path, then system PATH) and execute it silently:
   `innoextract.exe -d .\temp_triage_extract <target_binary>`
2. Recursively list all extracted `.exe`, `.dll`, and `.drv` files in the output folder.

#### Step 4: Import Address Table (IAT) Analysis (Network capability check)
For the main executable and any unsigned binaries extracted in Step 3:
1. Parse the PE Import Directory.
2. Check if the binary imports any networking/socket modules. Specifically scan the DLL imports list for:
   - `ws2_32.dll` (Winsock)
   - `wininet.dll`
   - `winhttp.dll`
   - `urlmon.dll`
3. Scan for Code Injection / Memory Patching API imports:
   - `VirtualAlloc`, `VirtualAllocEx`
   - `VirtualProtect`, `VirtualProtectEx`
   - `WriteProcessMemory`
   - `CreateRemoteThread`
4. Scan for Debugger Evasion imports:
   - `IsDebuggerPresent`, `CheckRemoteDebuggerPresent`
5. Record the presence of these APIs in the JSON report.

#### Step 5: Windows Defender Integration
1. Locate the local Windows Defender command-line utility:
   `C:\Program Files\Windows Defender\MpCmdRun.exe`
2. Programmatically launch it to scan the target file:
   `MpCmdRun.exe -Scan -ScanType 3 -File "<target_binary>"`
3. Parse the standard output to determine if threats were detected locally.

#### Step 6: JSON Output Generation
Output a structured JSON report to `stdout` containing the following structure:
```json
{
  "target_path": "E:\\Downloads\\Bnd.exe",
  "is_valid_pe": true,
  "signature": {
    "verified": false,
    "publisher": "n/a"
  },
  "package_type": "Inno Setup Installer",
  "local_av_scan": {
    "defender_threats_found": false,
    "raw_output": "..."
  },
  "extracted_components": [
    {
      "path": "extracted\\app\\bdcam.exe",
      "signature": {
        "verified": true,
        "publisher": "Bandicam Company Corp."
      },
      "imports_networking": false,
      "imports_injection_apis": false
    },
    {
      "path": "extracted\\app\\winspool.drv",
      "signature": {
        "verified": false,
        "publisher": "n/a"
      },
      "imports_networking": false,
      "imports_injection_apis": true
    }
  ]
}
```

### REQUIREMENTS
- Provide the complete C++ code in a single, well-commented source file.
- Handle file and process spawning securely (use `CreateProcessW` with quoted paths).
- Include appropriate error handling for missing files, unreadable headers, or subprocess failures.
```
