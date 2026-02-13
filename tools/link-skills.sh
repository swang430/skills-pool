#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
CONFIG_FILE="${REPO_ROOT}/config/targets.conf"

DRY_RUN=0
PRUNE=0
BACKUP_CONFLICTS=0
ONLY_PLATFORMS=""

usage() {
  cat <<'EOF'
用法:
  tools/link-skills.sh [选项]

选项:
  --config <path>         配置文件路径（默认: config/targets.conf）
  --only <a,b,c>          仅同步指定平台，例如: --only codex,gemini
  --prune                 删除目标目录里由本脚本管理但在源目录已不存在的软链接
  --backup-conflicts      遇到同名非软链接文件/目录时，自动改名备份后再创建软链接
  --dry-run               只打印将执行的操作，不真正修改
  -h, --help              显示帮助

配置格式:
  每行: platform|source_dir|target_dir
  例子:
    codex|dist/codex|~/.codex/skills
EOF
}

trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

resolve_path() {
  local raw_path="$1"
  if [[ "${raw_path}" == "~/"* ]]; then
    printf '%s/%s\n' "${HOME}" "${raw_path#\~/}"
  elif [[ "${raw_path}" == /* ]]; then
    printf '%s\n' "${raw_path}"
  else
    printf '%s/%s\n' "${REPO_ROOT}" "${raw_path}"
  fi
}

platform_allowed() {
  local platform="$1"
  if [[ -z "${ONLY_PLATFORMS}" ]]; then
    return 0
  fi

  local csv=",${ONLY_PLATFORMS},"
  if [[ "${csv}" == *",${platform},"* ]]; then
    return 0
  fi
  return 1
}

run_cmd() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf '[dry-run] %s\n' "$(printf '%q ' "$@")"
  else
    "$@"
  fi
}

link_one_skill() {
  local src_skill="$1"
  local dst_link="$2"
  local platform="$3"

  if [[ -L "${dst_link}" ]]; then
    local current_target
    current_target="$(readlink "${dst_link}" || true)"
    if [[ "${current_target}" == "${src_skill}" ]]; then
      printf '  - [%s] 已存在: %s -> %s\n' "${platform}" "${dst_link}" "${src_skill}"
      return 0
    fi
    run_cmd rm -f "${dst_link}"
  elif [[ -e "${dst_link}" ]]; then
    if [[ "${BACKUP_CONFLICTS}" -eq 1 ]]; then
      local backup_path="${dst_link}.bak.$(date +%Y%m%d%H%M%S)"
      run_cmd mv "${dst_link}" "${backup_path}"
      printf '  - [%s] 已备份冲突路径: %s\n' "${platform}" "${backup_path}"
    else
      printf '  - [%s] 跳过冲突(非软链接): %s\n' "${platform}" "${dst_link}" >&2
      return 0
    fi
  fi

  run_cmd ln -s "${src_skill}" "${dst_link}"
  printf '  - [%s] 已链接: %s -> %s\n' "${platform}" "${dst_link}" "${src_skill}"
}

prune_stale_links() {
  local src_dir="$1"
  local dst_dir="$2"
  local platform="$3"
  local seen_file="$4"

  shopt -s nullglob dotglob
  local candidate
  for candidate in "${dst_dir}"/*; do
    [[ -L "${candidate}" ]] || continue
    local skill_name
    skill_name="$(basename "${candidate}")"
    if grep -Fxq "${skill_name}" "${seen_file}"; then
      continue
    fi
    local link_target
    link_target="$(readlink "${candidate}" || true)"
    case "${link_target}" in
      "${src_dir}"/*)
        run_cmd rm -f "${candidate}"
        printf '  - [%s] 已清理过期链接: %s\n' "${platform}" "${candidate}"
        ;;
      *)
        ;;
    esac
  done
  shopt -u nullglob dotglob
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      CONFIG_FILE="$2"
      shift 2
      ;;
    --only)
      ONLY_PLATFORMS="$(trim "$2")"
      shift 2
      ;;
    --prune)
      PRUNE=1
      shift
      ;;
    --backup-conflicts)
      BACKUP_CONFLICTS=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf '未知参数: %s\n\n' "$1" >&2
      usage
      exit 1
      ;;
  esac
done

CONFIG_FILE="$(resolve_path "${CONFIG_FILE}")"
if [[ ! -f "${CONFIG_FILE}" ]]; then
  printf '配置文件不存在: %s\n' "${CONFIG_FILE}" >&2
  exit 1
fi

printf '读取配置: %s\n' "${CONFIG_FILE}"

while IFS='|' read -r raw_platform raw_source raw_target _; do
  raw_platform="$(trim "${raw_platform:-}")"
  raw_source="$(trim "${raw_source:-}")"
  raw_target="$(trim "${raw_target:-}")"

  [[ -z "${raw_platform}" ]] && continue
  [[ "${raw_platform}" == \#* ]] && continue
  [[ -z "${raw_source}" || -z "${raw_target}" ]] && continue

  if ! platform_allowed "${raw_platform}"; then
    continue
  fi

  src_dir="$(resolve_path "${raw_source}")"
  dst_dir="$(resolve_path "${raw_target}")"

  printf '\n[%s]\n' "${raw_platform}"
  printf '  source: %s\n' "${src_dir}"
  printf '  target: %s\n' "${dst_dir}"

  if [[ ! -d "${src_dir}" ]]; then
    printf '  - [%s] 源目录不存在，跳过\n' "${raw_platform}" >&2
    continue
  fi

  run_cmd mkdir -p "${dst_dir}"
  seen_file="$(mktemp)"
  trap 'rm -f "${seen_file}"' EXIT

  shopt -s nullglob dotglob
  for src_skill in "${src_dir}"/*; do
    [[ -d "${src_skill}" ]] || continue
    skill_name="$(basename "${src_skill}")"
    printf '%s\n' "${skill_name}" >> "${seen_file}"
    link_one_skill "${src_skill}" "${dst_dir}/${skill_name}" "${raw_platform}"
  done
  shopt -u nullglob dotglob

  if [[ "${PRUNE}" -eq 1 ]]; then
    prune_stale_links "${src_dir}" "${dst_dir}" "${raw_platform}" "${seen_file}"
  fi

  rm -f "${seen_file}"
  trap - EXIT
done < "${CONFIG_FILE}"
