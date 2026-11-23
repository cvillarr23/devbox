#!/bin/sh
# Consistent backup and restore of devbox persistent volumes.
#
#   devbox-backup.sh k8s backup  [-n NAMESPACE] [-d DEPLOYMENT] [--argo-app APP] [-o DIR]
#   devbox-backup.sh k8s restore DIR [-n NAMESPACE] [-d DEPLOYMENT] [--argo-app APP]
#   devbox-backup.sh docker backup  [-c CONTAINER] [-o DIR]
#   devbox-backup.sh docker restore DIR [-c CONTAINER]
#
# Devbox is stopped while its volumes are copied, so Chrome, Git and session state are
# quiescent. One gzip tarball per volume plus SHA256SUMS lands in DIR (default
# ~/backups/devbox/<target>-<UTC timestamp>), readable only by you: archives hold agent
# logins, Git and SSH configuration and the devbox access token. Restore replaces each
# volume's contents with its archive. See docs/operations.md.
set -eu

die() { echo "devbox-backup: $*" >&2; exit 1; }
log() { echo "devbox-backup: $*" >&2; }

NAMESPACE=devbox-trial DEPLOYMENT=devbox ARGO_APP= CONTAINER=devbox OUT= HELPER=devbox-backup-helper
ARGO_NAMESPACE=${ARGO_NAMESPACE:-argocd}
REPLICAS= ARGO_SYNC= RESTORE_DIR=

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      -n) NAMESPACE=$2; shift 2 ;;
      -d) DEPLOYMENT=$2; shift 2 ;;
      -c) CONTAINER=$2; shift 2 ;;
      -o) OUT=$2; shift 2 ;;
      --argo-app) ARGO_APP=$2; shift 2 ;;
      -*) die "unknown option $1" ;;
      *) [ -z "$RESTORE_DIR" ] || die "unexpected argument $1"; RESTORE_DIR=$1; shift ;;
    esac
  done
}

new_backup_dir() {
  dir=${OUT:-$HOME/backups/devbox/$1-$(date -u +%Y%m%dT%H%M%SZ)}
  [ ! -e "$dir" ] || die "$dir already exists"
  mkdir -p "$dir"
  chmod 700 "$dir"
  echo "$dir"
}

verify_checksums() {
  [ -f "$1/SHA256SUMS" ] || die "$1/SHA256SUMS missing"
  (cd "$1" && sha256sum --quiet -c SHA256SUMS) || die "checksum mismatch in $1"
}

# --- Kubernetes -------------------------------------------------------------

kube() { kubectl -n "$NAMESPACE" "$@"; }

deployment_pvcs() {
  kube get deployment "$DEPLOYMENT" -o jsonpath='{range .spec.template.spec.volumes[*]}{.persistentVolumeClaim.claimName}{"\n"}{end}' | sed '/^$/d'
}

deployment_selector() {
  kube get deployment "$DEPLOYMENT" -o go-template='{{range $k, $v := .spec.selector.matchLabels}}{{$k}}={{$v}},{{end}}' | sed 's/,$//'
}

# Argo CD self-heal would scale the deployment straight back up, so automated sync is
# paused for the copy and put back exactly as it was afterwards.
pause_argo_sync() {
  [ -n "$ARGO_APP" ] || return 0
  ARGO_SYNC=$(kubectl -n "$ARGO_NAMESPACE" get application "$ARGO_APP" -o jsonpath='{.spec.syncPolicy.automated}')
  [ -n "$ARGO_SYNC" ] || return 0
  log "pausing automated sync of Argo CD application $ARGO_APP"
  kubectl -n "$ARGO_NAMESPACE" patch application "$ARGO_APP" --type merge -p '{"spec":{"syncPolicy":{"automated":null}}}' >/dev/null
}

resume_argo_sync() {
  [ -n "$ARGO_SYNC" ] || return 0
  log "restoring automated sync of Argo CD application $ARGO_APP"
  kubectl -n "$ARGO_NAMESPACE" patch application "$ARGO_APP" --type merge -p "{\"spec\":{\"syncPolicy\":{\"automated\":$ARGO_SYNC}}}" >/dev/null
}

refuse_unpaused_argo() {
  tracked=$(kube get deployment "$DEPLOYMENT" -o jsonpath='{.metadata.annotations.argocd\.argoproj\.io/tracking-id}')
  [ -z "$tracked" ] || [ -n "$ARGO_APP" ] || die "deployment is managed by Argo CD; pass --argo-app ${tracked%%:*} so self-heal doesn't restart it mid-copy"
}

stop_deployment() {
  REPLICAS=$(kube get deployment "$DEPLOYMENT" -o jsonpath='{.spec.replicas}')
  log "scaling $NAMESPACE/$DEPLOYMENT from $REPLICAS to 0"
  kube scale deployment "$DEPLOYMENT" --replicas=0 >/dev/null
  kube wait --for=delete pod -l "$(deployment_selector)" --timeout=120s >/dev/null 2>&1 || true
  [ -z "$(kube get pod -l "$(deployment_selector)" -o name)" ] || die "devbox pods are still running"
}

start_deployment() {
  [ -n "$REPLICAS" ] || return 0
  log "scaling $NAMESPACE/$DEPLOYMENT back to $REPLICAS"
  kube scale deployment "$DEPLOYMENT" --replicas="$REPLICAS" >/dev/null
  kube rollout status deployment "$DEPLOYMENT" --timeout=300s >/dev/null || log "rollout not ready yet; check kubectl -n $NAMESPACE get pods"
}

helper_volumes() {
  sep=
  for pvc in $1; do
    printf '%s{"name":"%s","persistentVolumeClaim":{"claimName":"%s"}}' "$sep" "$pvc" "$pvc"
    sep=,
  done
}

helper_mounts() {
  sep=
  for pvc in $1; do
    printf '%s{"name":"%s","mountPath":"/pvc/%s"}' "$sep" "$pvc" "$pvc"
    sep=,
  done
}

# The helper runs the deployment's own image as UID/GID 1000 so restored files keep the
# ownership devbox expects. The rawfile PVs are node-local; the scheduler places it there.
start_helper() {
  image=$(kube get deployment "$DEPLOYMENT" -o jsonpath='{.spec.template.spec.containers[0].image}')
  kube delete pod "$HELPER" --ignore-not-found --wait >/dev/null
  kube apply -f - >/dev/null <<EOF
{"apiVersion":"v1","kind":"Pod","metadata":{"name":"$HELPER","labels":{"app.kubernetes.io/name":"devbox-backup"}},
 "spec":{"restartPolicy":"Never","automountServiceAccountToken":false,
  "securityContext":{"runAsUser":1000,"runAsGroup":1000,"fsGroup":1000,"runAsNonRoot":true,"seccompProfile":{"type":"RuntimeDefault"}},
  "containers":[{"name":"helper","image":"$image","command":["sleep","3600"],
   "securityContext":{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]}},
   "volumeMounts":[$(helper_mounts "$1")]}],
  "volumes":[$(helper_volumes "$1")]}}
EOF
  kube wait --for=condition=Ready pod/"$HELPER" --timeout=180s >/dev/null
}

stop_helper() { kube delete pod "$HELPER" --ignore-not-found --wait=false >/dev/null 2>&1 || true; }

k8s_cleanup() {
  stop_helper
  start_deployment
  resume_argo_sync
}

k8s_backup() {
  dir=$(new_backup_dir "$NAMESPACE")
  pvcs=$(deployment_pvcs)
  refuse_unpaused_argo
  trap k8s_cleanup EXIT
  pause_argo_sync
  stop_deployment
  start_helper "$pvcs"
  for pvc in $pvcs; do
    log "archiving PVC $pvc"
    kube exec "$HELPER" -- tar -C "/pvc/$pvc" --numeric-owner --exclude=./lost+found -czf - . > "$dir/$pvc.tar.gz"
  done
  (cd "$dir" && sha256sum -- *.tar.gz > SHA256SUMS)
  chmod 600 "$dir"/*
  echo "$dir"
}

# Restore never deletes lost+found: it belongs to root on a fresh filesystem.
k8s_restore() {
  [ -n "$RESTORE_DIR" ] || die "restore needs a backup directory"
  verify_checksums "$RESTORE_DIR"
  pvcs=$(deployment_pvcs)
  for pvc in $pvcs; do [ -f "$RESTORE_DIR/$pvc.tar.gz" ] || die "no archive for PVC $pvc"; done
  refuse_unpaused_argo
  trap k8s_cleanup EXIT
  pause_argo_sync
  stop_deployment
  start_helper "$pvcs"
  for pvc in $pvcs; do
    log "restoring PVC $pvc"
    kube exec -i "$HELPER" -- sh -c 'cd "$1" && find . -mindepth 1 -maxdepth 1 ! -name lost+found -exec rm -rf {} + && tar --numeric-owner -xzf -' sh "/pvc/$pvc" < "$RESTORE_DIR/$pvc.tar.gz"
  done
}

# --- Docker -----------------------------------------------------------------

container_volumes() {
  docker inspect -f '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}}{{"\n"}}{{end}}{{end}}' "$CONTAINER" | sed '/^$/d'
}

volume_tool() {
  image=$(docker inspect -f '{{.Config.Image}}' "$CONTAINER")
  vol=$1; shift
  docker run --rm -i --user 1000:1000 --network none -v "$vol:/v" --entrypoint sh "$image" -c "$@"
}

docker_cleanup() { docker start "$CONTAINER" >/dev/null && log "started $CONTAINER"; }

docker_backup() {
  dir=$(new_backup_dir "$CONTAINER")
  vols=$(container_volumes)
  log "stopping $CONTAINER"
  docker stop "$CONTAINER" >/dev/null
  trap docker_cleanup EXIT
  for vol in $vols; do
    log "archiving volume $vol"
    volume_tool "$vol" 'tar -C /v --numeric-owner -czf - .' > "$dir/$vol.tar.gz"
  done
  (cd "$dir" && sha256sum -- *.tar.gz > SHA256SUMS)
  chmod 600 "$dir"/*
  echo "$dir"
}

docker_restore() {
  [ -n "$RESTORE_DIR" ] || die "restore needs a backup directory"
  verify_checksums "$RESTORE_DIR"
  vols=$(container_volumes)
  for vol in $vols; do [ -f "$RESTORE_DIR/$vol.tar.gz" ] || die "no archive for volume $vol"; done
  log "stopping $CONTAINER"
  docker stop "$CONTAINER" >/dev/null
  trap docker_cleanup EXIT
  for vol in $vols; do
    log "restoring volume $vol"
    volume_tool "$vol" 'cd /v && find . -mindepth 1 -maxdepth 1 -exec rm -rf {} + && tar --numeric-owner -xzf -' < "$RESTORE_DIR/$vol.tar.gz"
  done
}

[ $# -ge 2 ] || die "usage: $0 k8s|docker backup|restore [DIR] [options]"
target=$1 action=$2
shift 2
parse_args "$@"
umask 077
case "$target $action" in
  "k8s backup") k8s_backup ;;
  "k8s restore") k8s_restore ;;
  "docker backup") docker_backup ;;
  "docker restore") docker_restore ;;
  *) die "unknown command: $target $action" ;;
esac
