#!/usr/bin/env python3
"""
Extract and Sort CC PDFs - Implementation v3.0 with CDFV2 Support

This script handles:
1. MIME-format MSG file parsing (v2.0)
2. CDFV2 binary MAPI MSG file parsing (NEW in v3.0)
3. Auto-detection of MSG format (MIME vs CDFV2)
4. PDF attachment extraction from both formats
5. Folder-based organization from email metadata
6. PDF decryption using qpdf
7. Bank/card type extraction from filenames
8. Duplicate detection using MD5 hashes
9. Organized folder structure creation
10. Verification checksums generation
"""

import os
import re
import subprocess
import json
import hashlib
from pathlib import Path
from collections import defaultdict
import shutil
from datetime import datetime

def calculate_md5(filepath):
    """Calculate MD5 hash of a file."""
    md5_hash = hashlib.md5()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b''):
            md5_hash.update(chunk)
    return md5_hash.hexdigest()

def detect_input_type(input_path):
    """
    Detect input type: MSG files, PDF files, or mixed.
    Returns: ('msg', 'pdf', or 'mixed')
    """
    input_path = Path(input_path)

    msg_count = len(list(input_path.rglob('*.msg')))
    pdf_count = len(list(input_path.rglob('*.pdf')))

    if msg_count > 0 and pdf_count == 0:
        return 'msg'
    elif pdf_count > 0 and msg_count == 0:
        return 'pdf'
    elif msg_count > 0 and pdf_count > 0:
        return 'mixed'
    else:
        return None

def detect_msg_format(msg_file):
    """
    Detect if MSG file is MIME format or CDFV2 binary MAPI format.
    Returns: ('mime', 'cdfv2', or None)
    """
    try:
        with open(msg_file, 'rb') as f:
            # Read first few bytes
            header = f.read(4)

            # CDFV2 starts with: 0xD0 0xCF 0x11 0xE0
            if header[:4] == b'\xd0\xcf\x11\xe0':
                return 'cdfv2'

            # MIME format typically starts with text
            if header[:5] == b'From ' or header[:1] in [b'R', b'D', b'S', b'C']:
                return 'mime'

            # Try reading as text
            try:
                f.seek(0)
                data = f.read(200).decode('utf-8', errors='ignore')
                if 'MIME-Version' in data or 'Content-Type' in data or 'From:' in data:
                    return 'mime'
            except:
                pass

            # If it looks like CDFV2 binary, classify as such
            if header[:4] == b'\xd0\xcf\x11\xe0':
                return 'cdfv2'

            return None
    except Exception as e:
        return None

def _unique_pdf_name(msg_file, attachment_filename, index=0):
    """
    Build a guaranteed-unique output filename:
      <msg_stem_60chars>_<4-char hash of full path>[_index]__<attachment_name>

    The 4-char hash of the full MSG path ensures files from different emails
    never collide even when their names truncate to the same 60 characters.
    """
    msg_stem = re.sub(r'[<>:"/\\|?*]', '_', Path(msg_file).stem)[:60]
    # Short hash of the full path so same-named emails get distinct prefixes
    path_hash = hashlib.md5(str(msg_file).encode()).hexdigest()[:4]
    att_name = Path(attachment_filename).name
    suffix = f"_{index}" if index else ""
    return f"{msg_stem}_{path_hash}{suffix}__{att_name}"


def extract_pdfs_from_cdfv2_msg(msg_file, extract_folder, folder_structure=None):
    """
    Extract PDF attachments from CDFV2 binary MAPI MSG format.

    Requires: pip install extract-msg (pure Python, works well with CDFV2)
    """
    try:
        import extract_msg as _extract_msg
    except ImportError:
        print(f"  [WARN] CDFV2 format detected but extract-msg not installed")
        print(f"     Install with: pip install extract-msg")
        print(f"     Skipping: {msg_file.name}")
        return []

    extracted_files = []

    try:
        # Parse CDFV2 MSG file (compatible with extract-msg 0.41+)
        msg = _extract_msg.openMsg(str(msg_file))

        # Extract metadata
        subject = msg.subject if hasattr(msg, 'subject') else 'Unknown'
        sender = msg.sender if hasattr(msg, 'sender') else 'Unknown'
        date = msg.date if hasattr(msg, 'date') else ''

        # Create subfolder based on metadata if requested
        if folder_structure == 'by_date' and date:
            try:
                # date may be a datetime object (extract-msg 0.41+) or a string
                if hasattr(date, 'strftime'):
                    msg_date = date
                else:
                    from email.utils import parsedate_to_datetime
                    msg_date = parsedate_to_datetime(str(date))
                subfolder = extract_folder / msg_date.strftime('%Y-%m')
            except:
                subfolder = extract_folder
        elif folder_structure == 'by_sender' and sender:
            # Extract sender name
            sender_name = re.sub(r'[<>:"/\\|?*]', '', sender.split('<')[0].strip())[:50]
            subfolder = extract_folder / sender_name if sender_name else extract_folder
        elif folder_structure == 'by_subject' and subject:
            # Extract subject keyword
            subject_name = re.sub(r'[<>:"/\\|?*]', '', subject)[:50]
            subfolder = extract_folder / subject_name if subject_name else extract_folder
        else:
            subfolder = extract_folder

        subfolder.mkdir(parents=True, exist_ok=True)

        # Extract attachments from CDFV2
        if hasattr(msg, 'attachments') and msg.attachments:
            for attachment in msg.attachments:
                try:
                    # extract-msg 0.41+: use longFilename; fall back to shortFilename or filename
                    filename = (
                        getattr(attachment, 'longFilename', None)
                        or getattr(attachment, 'shortFilename', None)
                        or getattr(attachment, 'filename', None)
                    )

                    if filename and filename.lower().endswith('.pdf'):
                        # extract-msg 0.41+: .data is bytes; older versions used .binary
                        data = getattr(attachment, 'data', None)
                        if data is None:
                            data = getattr(attachment, 'binary', None)
                        if data is None:
                            continue

                        # Prefix with MSG stem to avoid name collisions across emails
                        unique_name = _unique_pdf_name(msg_file, filename)
                        output_file = subfolder / unique_name
                        with open(output_file, 'wb') as f:
                            f.write(data)

                        extracted_files.append(output_file)
                except Exception as e:
                    pass  # Skip problematic attachments

        msg.close()

    except Exception as e:
        print(f"  [ERROR] Error extracting from CDFV2 {msg_file.name}: {str(e)[:50]}")

    return extracted_files

def extract_pdfs_from_mime_msg(msg_file, extract_folder, folder_structure=None):
    """
    Extract PDF attachments from MIME-format MSG file (v2.0 logic).
    """
    from email import policy
    from email.parser import BytesParser

    extracted_files = []

    try:
        with open(msg_file, 'rb') as f:
            msg = BytesParser(policy=policy.default).parse(f)

        # Extract metadata
        subject = msg.get('subject', 'Unknown')
        sender = msg.get('from', 'Unknown')
        date = msg.get('date', '')

        # Create subfolder based on metadata if requested
        if folder_structure == 'by_date' and date:
            try:
                from email.utils import parsedate_to_datetime
                msg_date = parsedate_to_datetime(date)
                subfolder = extract_folder / msg_date.strftime('%Y-%m')
            except:
                subfolder = extract_folder
        elif folder_structure == 'by_sender' and sender:
            sender_name = re.sub(r'[<>:"/\\|?*]', '', sender.split('<')[0].strip())[:50]
            subfolder = extract_folder / sender_name if sender_name else extract_folder
        elif folder_structure == 'by_subject' and subject:
            subject_name = re.sub(r'[<>:"/\\|?*]', '', subject)[:50]
            subfolder = extract_folder / subject_name if subject_name else extract_folder
        else:
            subfolder = extract_folder

        subfolder.mkdir(parents=True, exist_ok=True)

        # Extract attachments
        if msg.is_multipart():
            for part in msg.iter_parts():
                filename = part.get_filename()

                if filename and filename.lower().endswith('.pdf'):
                    payload = part.get_payload(decode=True)
                    # Prefix with MSG stem to avoid name collisions across emails
                    unique_name = _unique_pdf_name(msg_file, filename)
                    output_file = subfolder / unique_name

                    with open(output_file, 'wb') as f:
                        f.write(payload)

                    extracted_files.append(output_file)

    except Exception as e:
        pass  # Handled by caller

    return extracted_files

def extract_pdfs_from_msg(msg_file, extract_folder, folder_structure=None):
    """
    Extract PDF attachments from MSG file (auto-detects format).
    Supports both MIME and CDFV2 formats.
    """
    # Detect MSG format
    msg_format = detect_msg_format(msg_file)

    if msg_format == 'cdfv2':
        return extract_pdfs_from_cdfv2_msg(msg_file, extract_folder, folder_structure)
    elif msg_format == 'mime':
        return extract_pdfs_from_mime_msg(msg_file, extract_folder, folder_structure)
    else:
        # Try MIME first, then CDFV2
        result = extract_pdfs_from_mime_msg(msg_file, extract_folder, folder_structure)
        if result:
            return result
        return extract_pdfs_from_cdfv2_msg(msg_file, extract_folder, folder_structure)

def extract_all_pdfs_from_msgs(msg_folder, extract_folder, folder_structure=None):
    """
    Extract PDFs from all MSG files in folder (supports both MIME and CDFV2).
    """
    extract_folder = Path(extract_folder)
    extract_folder.mkdir(parents=True, exist_ok=True)

    msg_folder = Path(msg_folder)
    all_extracted = []

    msg_files = list(msg_folder.rglob('*.msg'))
    print(f"Found {len(msg_files)} MSG files (auto-detecting format)")

    mime_count = 0
    cdfv2_count = 0

    for i, msg_file in enumerate(msg_files, 1):
        msg_format = detect_msg_format(msg_file)

        if msg_format == 'cdfv2':
            cdfv2_count += 1
        elif msg_format == 'mime':
            mime_count += 1

        extracted = extract_pdfs_from_msg(msg_file, extract_folder, folder_structure)

        status = f"[{msg_format.upper()}]" if msg_format else "[?]"
        if extracted:
            print(f"  [{i:3d}/{len(msg_files)}] {status} {msg_file.name[:50]}: {len(extracted)} PDFs")
            all_extracted.extend(extracted)

    print(f"\nFormat breakdown: {mime_count} MIME, {cdfv2_count} CDFV2, {len(msg_files)-mime_count-cdfv2_count} unknown")
    print(f"Total PDFs extracted: {len(all_extracted)}")
    return all_extracted

def find_passwords(search_folder):
    """
    Find passwords from the input folder. Supports two formats:

    Format 1 - Single password: a .txt file whose filename stem is the password.
        e.g.  HARS2806.txt  ->  password list = ['HARS2806']

    Format 2 - Multiple passwords: a file named 'passwords.txt' with one password
        per line (blank lines and lines starting with # are ignored).
        e.g.  passwords.txt containing:
                  HARS2806
                  HAR28061
                  INABM2806
                  INABM123

    Returns a list of passwords to try, or an empty list if none found.
    """
    search_path = Path(search_folder)

    # Check for passwords.txt (multi-password file) first.
    # Search input folder, its parent, and grandparent — so passwords.txt is
    # found regardless of whether input is TestMails, TestMails/CCEmails,
    # or TestMails_Output/CCEmails, etc.
    for search_dir in [search_path, search_path.parent, search_path.parent.parent]:
        pw_file = search_dir / 'passwords.txt'
        if pw_file.exists():
            lines = pw_file.read_text(encoding='utf-8').splitlines()
            passwords = [l.strip() for l in lines if l.strip() and not l.strip().startswith('#')]
            if passwords:
                print(f"Found {len(passwords)} passwords in {pw_file}")
                return passwords

    # Fall back to single-password .txt filename stem
    for search_dir in [search_path, search_path.parent, search_path.parent.parent]:
        for txt_file in search_dir.glob('*.txt'):
            if txt_file.stem:
                print(f"Found password from filename: {txt_file.stem}")
                return [txt_file.stem]

    return []

def extract_bank_and_card_type(filename):
    """Extract bank name and card type from PDF filename."""
    filename_lower = filename.lower()

    # Check MSG-prefixed filename (has __ separator) — also check the attachment part alone
    # e.g. "Your Indian Oil Axis Bank..._a1b2__Credit Card Statement.pdf"
    # We match against the full unique name so MSG subject keywords help identify the bank.
    patterns = {
        ('Axis', 'Flipkart'):   r'flipkart.*axis|axis.*flipkart|flipkart',
        ('Axis', 'IndianOil'):  r'indian\s*oil.*axis|axis.*indian\s*oil|indian.?oil',
        ('HDFC', 'Regalia'):    r'hdfc.*regalia|regalia.*hdfc|regalia',
        ('HDFC', 'TataNeu'):    r'hdfc.*tata\s*neu|tata\s*neu.*hdfc|tata.?neu',
        ('ICICI', 'Amazon'):    r'amazon.*icici|icici.*amazon|amazon.*pay',
        ('ICICI', 'Sapphiro'):  r'icici.*sapphiro|sapphiro.*icici|sapphiro|4315|3747',
        ('SBI', 'BPCL-Octane'): r'bpcl|sbi.*octane|octane.*sbi|9435',
        ('SBM', 'Global'):      r'sbm.*global|global.*sbm|niyo.*global|sbniyocr|global.credit|your.global.credit',
        ('HSBC', 'Premier'):    r'hsbc.*premier|premier.*hsbc|hsbc',
        ('YES', 'Reserv'):      r'yes.?bank.*reserv|reserv.*yes|yes_bank_reserv|400054',
    }

    for (bank, card_type), pattern in patterns.items():
        if re.search(pattern, filename_lower):
            return bank, card_type

    return None, None

def decrypt_pdf(pdf_path, passwords, output_path):
    """
    Decrypt PDF using qpdf, trying each password in the list until one succeeds.
    Returns the password that worked, or None if all failed.
    """
    if isinstance(passwords, str):
        passwords = [passwords]  # backward compatibility

    # Always try empty password first (handles unencrypted or owner-only PDFs)
    all_passwords = [''] + list(passwords)

    last_error = ''
    for password in all_passwords:
        # Try each password with multiple qpdf flag combinations to handle malformed PDFs
        flag_sets = [
            [],                              # standard
            ['--warning-exit-0'],            # ignore warnings that inflate exit code
            ['--ignore-xref-streams'],       # malformed xref
            ['--warning-exit-0', '--ignore-xref-streams'],
        ]
        for extra_flags in flag_sets:
            try:
                cmd = ['qpdf', '--password=' + password] + extra_flags + ['--decrypt', str(pdf_path), str(output_path)]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                # Exit code 0 = success, 3 = success with warnings
                if result.returncode in (0, 3):
                    return password if password else '(empty)'
                last_error = (result.stderr or result.stdout or '').strip()
            except Exception as e:
                last_error = str(e)
                continue

    return None  # all passwords failed

def find_duplicates(pdf_folder):
    """Find duplicate PDFs using MD5 hash comparison."""
    hashes = defaultdict(list)

    for pdf_file in Path(pdf_folder).rglob('*.pdf'):
        file_hash = calculate_md5(str(pdf_file))
        hashes[file_hash].append(pdf_file)

    duplicates = {h: files for h, files in hashes.items() if len(files) > 1}
    return duplicates

def organize_pdfs(input_folder, output_folder, password=None, extract_msg=False, folder_structure=None):
    """
    Main workflow:
    1. Extract PDFs from MSG files (both MIME and CDFV2 formats, if needed)
    2. Decrypt PDFs
    3. Organize by bank/card type
    4. Remove duplicates
    5. Generate checksums
    """

    input_path = Path(input_folder)
    output_path = Path(output_folder)

    results = {
        'total_analyzed': 0,
        'msg_files_processed': 0,
        'msg_format_breakdown': {'mime': 0, 'cdfv2': 0, 'unknown': 0},
        'pdfs_extracted_from_msg': 0,
        'successfully_decrypted': 0,
        'failed_decryption': [],
        'duplicates_found': 0,
        'duplicates_removed': 0,
        'organization': defaultdict(int),
        'checksums': {},
        'input_type': None
    }

    # Detect input type
    input_type = detect_input_type(input_path)
    results['input_type'] = input_type
    print(f"Input type detected: {input_type}")

    # Extract PDFs from MSG if needed
    if input_type in ['msg', 'mixed'] or extract_msg:
        print(f"\nExtracting PDFs from MSG files (auto-detecting format)...")
        temp_extract = output_path / '_temp_msg_extract'
        # Always start fresh to avoid stale files from a previous run
        if temp_extract.exists():
            shutil.rmtree(temp_extract)
        extracted_pdfs = extract_all_pdfs_from_msgs(input_path, temp_extract, folder_structure)

        results['msg_files_processed'] = len(list(input_path.rglob('*.msg')))
        results['pdfs_extracted_from_msg'] = len(extracted_pdfs)

        # Count format breakdown
        for msg_file in input_path.rglob('*.msg'):
            fmt = detect_msg_format(msg_file)
            if fmt == 'mime':
                results['msg_format_breakdown']['mime'] += 1
            elif fmt == 'cdfv2':
                results['msg_format_breakdown']['cdfv2'] += 1
            else:
                results['msg_format_breakdown']['unknown'] += 1

        pdf_files = extracted_pdfs
        search_folder = input_path  # always search original input folder for passwords.txt
    else:
        pdf_files = list(input_path.rglob('*.pdf'))
        search_folder = input_path
        temp_extract = None

    # Clean output directories — wipe previous run's results so we never
    # accumulate stale files across runs (same logic as temp folder wiping).
    decrypted_dir = output_path / 'Decrypted_PDFs_Correct'
    duplicates_dir = output_path / 'Duplicates_Backup'
    for clean_dir in [decrypted_dir, duplicates_dir]:
        if clean_dir.exists():
            shutil.rmtree(clean_dir)
            print(f"[Cleanup] Removed previous: {clean_dir.name}/")
    decrypted_dir.mkdir(parents=True, exist_ok=True)
    duplicates_dir.mkdir(parents=True, exist_ok=True)

    results['total_analyzed'] = len(pdf_files)
    print(f"\nProcessing {len(pdf_files)} PDFs...")

    # Build password list
    if password:
        # Password(s) passed via CLI: allow comma-separated or single value
        passwords = [p.strip() for p in password.split(',') if p.strip()]
    else:
        passwords = find_passwords(search_folder)
        if not passwords:
            raise ValueError(
                "No passwords found. Either:\n"
                "  1. Pass --password PW1,PW2,PW3 on the command line, or\n"
                "  2. Create a 'passwords.txt' file in the input folder with one password per line"
            )

    print(f"Will try {len(passwords)} password(s) per PDF: {', '.join(passwords)}")

    # Create temporary decrypted folder — always start fresh to avoid stale-file collisions
    temp_decrypted = output_path / '_temp_decrypted'
    if temp_decrypted.exists():
        shutil.rmtree(temp_decrypted)
    temp_decrypted.mkdir(parents=True, exist_ok=True)

    # Decrypt PDFs - try each password until one works
    decrypted_map = {}

    for i, pdf_path in enumerate(pdf_files, 1):
        temp_output = temp_decrypted / pdf_path.name

        working_pw = decrypt_pdf(str(pdf_path), passwords, str(temp_output))
        if working_pw:
            decrypted_map[pdf_path] = temp_output
            results['successfully_decrypted'] += 1
            print(f"  [{i}/{len(pdf_files)}] [OK] Decrypted: {pdf_path.name[:50]} (pw: {working_pw})")
        else:
            results['failed_decryption'].append(pdf_path.name)
            print(f"  [{i}/{len(pdf_files)}] [FAIL] All passwords failed: {pdf_path.name[:50]}")

    # Find duplicates
    duplicates = find_duplicates(str(temp_decrypted))
    results['duplicates_found'] = sum(len(files) - 1 for files in duplicates.values())

    # Process files: organize and handle duplicates
    processed_hashes = set()

    for original_pdf in pdf_files:
        if original_pdf not in decrypted_map:
            continue

        decrypted_pdf = decrypted_map[original_pdf]
        if not decrypted_pdf.exists():
            print(f"  [WARN] Decrypted file missing (skipping): {decrypted_pdf.name}")
            continue
        file_hash = calculate_md5(str(decrypted_pdf))

        # Extract bank and card type
        bank, card_type = extract_bank_and_card_type(original_pdf.name)
        if not bank or not card_type:
            bank, card_type = 'Unknown', 'Unknown'

        bank_folder = decrypted_dir / f"{bank}-{card_type}"
        bank_folder.mkdir(parents=True, exist_ok=True)

        # Check if this is a duplicate
        is_duplicate = False
        for hash_val, files in duplicates.items():
            if file_hash == hash_val and len(files) > 1:
                if hash_val not in processed_hashes:
                    processed_hashes.add(hash_val)
                    final_output = bank_folder / decrypted_pdf.name
                    shutil.move(str(decrypted_pdf), str(final_output))
                else:
                    is_duplicate = True
                    results['duplicates_removed'] += 1
                break

        if is_duplicate:
            dup_folder = duplicates_dir / f"{bank}-{card_type}"
            dup_folder.mkdir(parents=True, exist_ok=True)
            shutil.move(str(decrypted_pdf), str(dup_folder / decrypted_pdf.name))
        elif original_pdf not in decrypted_map or file_hash not in processed_hashes:
            if original_pdf in decrypted_map:
                final_output = bank_folder / decrypted_pdf.name
                if not final_output.exists():
                    shutil.move(str(decrypted_pdf), str(final_output))

        results['organization'][f"{bank}-{card_type}"] += 1
        results['checksums'][str(decrypted_pdf.relative_to(output_path))] = file_hash

    # Cleanup temp folders
    shutil.rmtree(temp_decrypted, ignore_errors=True)
    if temp_extract:
        shutil.rmtree(temp_extract, ignore_errors=True)

    # Save checksums
    checksums_file = output_path / 'verification_checksums.json'
    with open(checksums_file, 'w') as f:
        json.dump(results['checksums'], f, indent=2)

    return results

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python extract_sort_cc_pdfs.py <input_folder> [output_folder] [password] [--extract-msg] [--folder-by-date|--folder-by-sender|--folder-by-subject]")
        print("\nPassword options (pick one):")
        print("  Single password:    pass as 3rd argument, e.g. HARS2806")
        print("  Multiple passwords: pass comma-separated, e.g. HARS2806,HAR28061,INABM123")
        print("  From file:          place 'passwords.txt' in input folder, one password per line")
        print("  Single from file:   place HARS2806.txt in input folder (filename stem = password)")
        print("\nOptions:")
        print("  --extract-msg         Extract PDFs from MSG files")
        print("  --folder-by-date      Organize extracted PDFs by date (YYYY-MM)")
        print("  --folder-by-sender    Organize extracted PDFs by sender")
        print("  --folder-by-subject   Organize extracted PDFs by subject")
        print("\nv3.0 Features:")
        print("  - Supports both MIME and CDFV2 binary MSG formats")
        print("  - Auto-detects MSG format automatically")
        print("  - Multi-password support: tries all passwords per PDF")
        print("  - Requires extract-msg for CDFV2 support: pip install extract-msg")
        sys.exit(1)

    input_folder = sys.argv[1]
    output_folder = sys.argv[2] if len(sys.argv) > 2 else None
    password = sys.argv[3] if len(sys.argv) > 3 else None

    extract_msg = '--extract-msg' in sys.argv
    folder_structure = None

    if '--folder-by-date' in sys.argv:
        folder_structure = 'by_date'
    elif '--folder-by-sender' in sys.argv:
        folder_structure = 'by_sender'
    elif '--folder-by-subject' in sys.argv:
        folder_structure = 'by_subject'

    if not output_folder:
        output_folder = Path(input_folder).parent / 'Organized_PDFs'

    input_type = detect_input_type(input_folder)
    if input_type in ['msg', 'mixed']:
        extract_msg = True

    results = organize_pdfs(input_folder, output_folder, password, extract_msg, folder_structure)

    print("\n" + "=" * 70)
    print("EXTRACTION & ORGANIZATION SUMMARY")
    print("=" * 70)
    print(f"\nInput type: {results['input_type']}")

    if results['msg_files_processed'] > 0:
        print(f"MSG files processed: {results['msg_files_processed']}")
        print(f"  MIME format: {results['msg_format_breakdown']['mime']}")
        print(f"  CDFV2 format: {results['msg_format_breakdown']['cdfv2']}")
        print(f"  Unknown format: {results['msg_format_breakdown']['unknown']}")
        print(f"PDFs extracted from MSG: {results['pdfs_extracted_from_msg']}")

    print(f"\nTotal PDFs analyzed: {results['total_analyzed']}")
    print(f"Successfully decrypted: {results['successfully_decrypted']}")
    print(f"Failed decryption: {len(results['failed_decryption'])}")

    if results['failed_decryption']:
        print(f"\nFailed files:")
        for fname in results['failed_decryption'][:10]:
            print(f"  - {fname}")
        if len(results['failed_decryption']) > 10:
            print(f"  ... and {len(results['failed_decryption'])-10} more")

    print(f"\nDuplicates found: {results['duplicates_found']}")
    print(f"Duplicates removed: {results['duplicates_removed']}")

    print(f"\nOrganization summary:")
    for bank_card, count in sorted(results['organization'].items()):
        print(f"  {bank_card}: {count} PDFs")

    print(f"\nOutput structure:")
    print(f"  Decrypted_PDFs_Correct/ - {sum(results['organization'].values())} organized PDFs")
    print(f"  Duplicates_Backup/ - {results['duplicates_removed']} archived duplicates")
    print(f"  verification_checksums.json - MD5 hashes for {len(results['checksums'])} files")
