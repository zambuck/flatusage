#!/bin/sh
# redact_data_usage.sh
#
# POSIX sh + awk rewrite of redact_data_usage.py
#
# Usage:
#   ./redact_data_usage.sh input.csv output.csv
#   ./redact_data_usage.sh input.csv output.csv --mode mask
#   ./redact_data_usage.sh input.csv output.csv --mode hash --salt mysalt
#   ./redact_data_usage.sh input.csv output.csv --fields AccountNumber NMI DeviceNumber

set -e

input=""
output=""
fields="AccountNumber NMI DeviceNumber"
mode="hash"
salt=""

usage() {
    cat <<'EOF'
Usage: redact_data_usage.sh input.csv output.csv [options]

Options:
  --fields F1 F2 ...   column names to redact
                       (default: AccountNumber NMI DeviceNumber)
  --mode hash|mask     redaction mode (default: hash)
  --salt STRING        fixed salt for hash mode
  -h, --help           show this help
EOF
}

# ------------------- argument parsing -------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --fields)
            shift
            fields=""
            while [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; do
                fields="$fields $1"
                shift
            done
            ;;
        --mode)
            shift
            mode="$1"
            shift
            ;;
        --salt)
            shift
            salt="$1"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            break
            ;;
        -*)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
        *)
            if [ -z "$input" ]; then
                input="$1"
            elif [ -z "$output" ]; then
                output="$1"
            else
                echo "Unexpected argument: $1" >&2
                usage >&2
                exit 1
            fi
            shift
            ;;
    esac
done

fields="${fields# }"

# ------------------- validation -------------------
case "$mode" in
    hash|mask) ;;
    *)
        echo "Invalid mode: $mode" >&2
        usage >&2
        exit 1
        ;;
esac

if [ -z "$input" ] || [ -z "$output" ]; then
    echo "input.csv and output.csv are required." >&2
    usage >&2
    exit 1
fi

if [ ! -f "$input" ]; then
    echo "Input file not found: $input" >&2
    exit 1
fi

if [ ! -r "$input" ]; then
    echo "Cannot read input file: $input" >&2
    exit 1
fi

if [ ! -s "$input" ]; then
    echo "Input file appears empty: $input" >&2
    exit 1
fi

# ------------------- hash-tool detection -------------------
hashcmd=""
if [ "$mode" = "hash" ]; then
    if command -v sha256sum >/dev/null 2>&1; then
        hashcmd="sha256sum | awk '{print $1}'"
    elif command -v shasum >/dev/null 2>&1; then
        hashcmd="shasum -a 256 | awk '{print $1}'"
    elif command -v openssl >/dev/null 2>&1; then
        hashcmd="openssl dgst -sha256 | awk '{print $NF}'"
    else
        echo "hash mode requires a SHA-256 utility (sha256sum, shasum, or openssl)." >&2
        exit 1
    fi

    if [ -z "$salt" ]; then
        salt="$(date +%s 2>/dev/null)$$"
        echo "No --salt given; using random salt '$salt' for this run. Pass --salt to reproduce the same pseudonyms across runs." >&2
    fi
fi

# ------------------- run redaction -------------------
tmpout="${output}.tmp.$$"
trap 'rm -f "$tmpout"' EXIT

awk -v "mode=$mode" \
    -v "fields=$fields" \
    -v "salt=$salt" \
    -v "outfile=$tmpout" \
    -v "hashcmd=$hashcmd" \
    -f - "$input" <<'AWKEOF' || { echo "Redaction failed." >&2; exit 1; }

function sh_quote(s,    x) {
    x = s
    gsub(/'/, "'\\''", x)
    return "'" x "'"
}

function quote_count(s,    t) {
    t = s
    gsub(/""/, "", t)
    return gsub(/"/, "", t)
}

function repeat_char(c, n,    s, i) {
    s = ""
    for (i = 1; i <= n; i++) s = s c
    return s
}

function parse_csv(line, raw, val,     n, i, c, field, inquote, v) {
    n = 0
    field = ""
    inquote = 0
    for (i = 1; i <= length(line); i++) {
        c = substr(line, i, 1)
        if (inquote) {
            if (c == "\"") {
                if (substr(line, i + 1, 1) == "\"") {
                    field = field "\""
                    i++
                } else {
                    inquote = 0
                }
            } else {
                field = field c
            }
        } else {
            if (c == ",") {
                n++
                raw[n] = field
                field = ""
            } else if (c == "\"") {
                inquote = 1
            } else {
                field = field c
            }
        }
    }
    n++
    raw[n] = field

    for (i = 1; i <= n; i++) {
        v = raw[i]
        if (v ~ /^".*"$/ && length(v) >= 2) {
            gsub(/^"|"$/, "", v)
            gsub(/""/, "\"", v)
        }
        val[i] = v
    }
    return n
}

function csv_encode(v,    out) {
    if (v ~ /[,\""\n\r]/) {
        out = v
        gsub(/"/, "\"\"", out)
        return "\"" out "\""
    }
    return v
}

function warn(msg) {
    system("printf '%s\\n' " sh_quote(msg) " >&2")
}

function hash_redact(field, value,     prefix, cmd, digest, key) {
    if (value == "") return ""
    key = field "\034" value
    if (key in hashcache) return hashcache[key]

    prefix = field
    gsub(/[^A-Za-z]/, "", prefix)
    prefix = toupper(prefix)
    if (length(prefix) > 4) prefix = substr(prefix, 1, 4)
    if (prefix == "") prefix = "VAL"

    cmd = "printf '%s' " sh_quote(salt field value) " | " hashcmd
    if ((cmd | getline digest) <= 0) digest = "0"
    close(cmd)

    hashcache[key] = prefix "_" substr(digest, 1, 10)
    return hashcache[key]
}

function mask_redact(value,     n, pre) {
    if (value == "") return ""
    n = length(value)
    if (n <= 4) return repeat_char("X", n)
    pre = repeat_char("X", n - 4)
    return pre substr(value, n - 3)
}

function write_row(arr, n, f,     i) {
    for (i = 1; i <= n; i++) {
        printf "%s", csv_encode(arr[i]) > f
        if (i < n) printf "," > f
    }
    printf "\n" > f
}

BEGIN {
    t_count = split(fields, t_orig)
    for (i = 1; i <= t_count; i++) {
        t_low[i] = tolower(t_orig[i])
        t_hit[i] = 0
    }
}

NR == 1 {
    header_seen = 1
    header_line = $0
    gsub(/\r$/, "", header_line)

    nf = parse_csv(header_line, h_raw, h_val)

    for (i = 1; i <= nf; i++) {
        fname[i] = h_val[i]
        low = tolower(h_val[i])
        redact[i] = 0
        for (j = 1; j <= t_count; j++) {
            if (low == t_low[j]) {
                redact[i] = 1
                t_hit[j] = 1
                if (matched == "") matched = fname[i]
                else matched = matched ", " fname[i]
                break
            }
        }
    }

    for (j = 1; j <= t_count; j++) {
        if (!t_hit[j]) {
            warn("Warning: requested field '" t_orig[j] "' not found in CSV header and will be skipped.")
        }
    }

    write_row(h_val, nf, outfile)
    next
}

{
    line = $0
    gsub(/\r$/, "", line)

    while (quote_count(line) % 2 == 1 && (getline nextline) > 0) {
        gsub(/\r$/, "", nextline)
        line = line ORS nextline
    }

    nf2 = parse_csv(line, r_raw, r_val)
    for (i = 1; i <= nf2; i++) {
        if (redact[i]) {
            if (mode == "hash") r_val[i] = hash_redact(fname[i], r_val[i])
            else r_val[i] = mask_redact(r_val[i])
        }
    }

    write_row(r_val, nf2, outfile)
    rows++
}

END {
    if (!header_seen) {
        warn("Error: Input file has no header row / appears empty.")
        exit 1
    }
    close(outfile)
    printf "Redacted %d row(s). Fields redacted: %s\n", rows, (matched == "" ? "(none)" : matched)
}
AWKEOF

mv "$tmpout" "$output"
trap - EXIT
echo "Output written to: $output"
