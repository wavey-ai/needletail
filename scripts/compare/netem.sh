#!/bin/bash
# Impair the network of a viewer host in both directions, the same way for
# every system. SSH is left alone, so that the host stays reachable.
#
#   netem.sh apply "loss 1%"            packets in and out are dropped at random
#   netem.sh apply "loss gemodel 1% 20%"  bursts: mean length 5 packets, 4.8% lost
#   netem.sh apply "delay 100ms"        100 ms each way
#   netem.sh clear
#   netem.sh show
#
# Outgoing packets pass a netem qdisc on the interface. Incoming packets are
# redirected to an ifb device that carries the same netem. Both IPv4 and IPv6
# are impaired; only IPv4 TCP port 22 is exempt.
set -euo pipefail
DEV=${NETEM_DEV:-eth0}
IFB=ifb0

clear_all() {
  tc qdisc del dev "$DEV" root 2>/dev/null || true
  tc qdisc del dev "$DEV" ingress 2>/dev/null || true
  tc qdisc del dev "$IFB" root 2>/dev/null || true
}

case "${1:-}" in
  apply)
    spec=${2:?netem options}
    modprobe sch_netem
    modprobe ifb numifbs=1 2>/dev/null || true
    ip link show "$IFB" >/dev/null 2>&1 || ip link add "$IFB" type ifb
    ip link set "$IFB" up
    clear_all
    # Out: everything goes to band 3 (netem) except SSH, which goes to band 1.
    tc qdisc add dev "$DEV" root handle 1: prio bands 3 priomap 2 2 2 2 2 2 2 2 2 2 2 2 2 2 2 2
    # shellcheck disable=SC2086
    tc qdisc add dev "$DEV" parent 1:3 handle 30: netem limit 20000 $spec
    tc filter add dev "$DEV" parent 1: protocol ip prio 1 u32 \
      match ip protocol 6 0xff match ip sport 22 0xffff flowid 1:1
    # In: SSH passes, the rest is redirected through the ifb device.
    tc qdisc add dev "$DEV" handle ffff: ingress
    tc filter add dev "$DEV" parent ffff: protocol ip prio 1 u32 \
      match ip protocol 6 0xff match ip dport 22 0xffff action pass
    tc filter add dev "$DEV" parent ffff: protocol all prio 2 matchall \
      action mirred egress redirect dev "$IFB"
    # shellcheck disable=SC2086
    tc qdisc add dev "$IFB" root handle 1: netem limit 20000 $spec
    echo "netem on $DEV in and out: $spec"
    ;;
  clear)
    clear_all
    echo "netem cleared on $DEV"
    ;;
  show)
    tc -s qdisc show dev "$DEV" | grep -A2 netem || echo "no netem on $DEV out"
    tc -s qdisc show dev "$IFB" 2>/dev/null | grep -A2 netem || echo "no netem on $DEV in"
    ;;
  *)
    echo "usage: netem.sh apply 'NETEM OPTIONS' | clear | show" >&2
    exit 2
    ;;
esac
