"""Fixed, configuration-owned /proc activity probes."""


class LocalActivityError(Exception):
    pass


class ConfiguredLocalActivityMonitor:
    def __init__(self, probes=(), proc_root="/proc"):
        self.probes = tuple(probes)
        self.proc_root = proc_root

    def active(self):
        labels = []
        try:
            for probe in self.probes:
                if probe.kind == "process_name" and self._process_active(probe.value): labels.append(
                    f"process:{probe.value}")
                if probe.kind == "tcp_listener" and self._port_active(probe.value): labels.append(
                    f"tcp-listener:{probe.value}")
        except (OSError, UnicodeError, ValueError) as error:
            raise LocalActivityError() from error
        return tuple(sorted(labels))

    def _process_active(self, name):
        from pathlib import Path
        for child in Path(self.proc_root).iterdir():
            if child.name.isdigit() and (child / "comm").read_text().strip() == name:
                return True
        return False

    def _port_active(self, port):
        from pathlib import Path
        for name in ("tcp", "tcp6"):
            for line in (Path(self.proc_root) / "net" / name).read_text().splitlines()[1:]:
                fields = line.split()
                if len(fields) >= 4 and fields[3] == "0A" and int(fields[1].split(":")[1], 16) == port:
                    return True
        return False
