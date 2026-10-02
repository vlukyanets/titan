# Node setup: the encrypted Docker volume

Every TITAN node keeps all of TITAN's data inside an encrypted volume
([ADR 0007](../adr/0007-sensitive-data-protection.md)). TITAN runs only in
Docker and stores everything in named volumes (`db-data`, `ntfy-cache`) or in
memory (`tmpfs`), so the encrypted volume holds Docker's whole data root: the
database, the ntfy cache, container logs and image layers. The Compose file
never bind-mounts host directories for data, so nothing lands outside it.

The rest of the system disk need not be encrypted, with one exception: swap,
because the kernel can write database pages to it.

## Linux: LUKS2

The volume can be a partition (preferred) or a file on an existing disk.

```bash
sudo systemctl stop docker.socket docker.service containerd.service

# A partition...
DEV=/dev/nvme0n1p4
# ...or a file, if there is no spare partition:
#   sudo fallocate -l 100G /var/lib/titan-crypt.img && DEV=/var/lib/titan-crypt.img

sudo cryptsetup luksFormat --type luks2 "$DEV"
sudo cryptsetup open "$DEV" titan-crypt
sudo mkfs.ext4 -L titan-data /dev/mapper/titan-crypt
sudo mkdir -p /srv/titan-crypt
sudo mount /dev/mapper/titan-crypt /srv/titan-crypt
sudo mkdir -p /srv/titan-crypt/docker /srv/titan-crypt/containerd

# Existing Docker data, if any, moves over.
sudo rsync -aHAX /var/lib/docker/ /srv/titan-crypt/docker/
sudo rsync -aHAX /var/lib/containerd/ /srv/titan-crypt/containerd/
```

Docker keeps volumes, containers and logs under `/var/lib/docker`, and with
the containerd image store also container layers under `/var/lib/containerd`.
Both are bind-mounted from the encrypted volume, so no Docker configuration
changes.

`/etc/crypttab` (use the path instead of `UUID=` for a file):

```text
titan-crypt  UUID=<uuid from blkid -s UUID -o value $DEV>  none  tpm2-device=auto
```

`/etc/fstab`:

```text
/dev/mapper/titan-crypt        /srv/titan-crypt     ext4  defaults,nofail  0 2
/srv/titan-crypt/docker        /var/lib/docker      none  bind,nofail      0 0
/srv/titan-crypt/containerd    /var/lib/containerd  none  bind,nofail      0 0
```

`nofail` lets the machine boot when the volume stays locked; Docker must then
not start on the empty directories underneath. `sudo systemctl edit
docker.service` and `sudo systemctl edit containerd.service`, each with:

```ini
[Unit]
RequiresMountsFor=/var/lib/docker /var/lib/containerd
```

### Unlocking at boot

Enrol a recovery key first and keep it offline, for example in a password
manager, never on the node itself:

```bash
sudo systemd-cryptenroll --recovery-key "$DEV"
```

Then the unlock method that fits the node:

| Node | Unlock | Command |
|---|---|---|
| Home server | TPM2 and Tang together: back by itself after a power cut, but only on the home network | `clevis luks bind -d "$DEV" sss '{"t":2,"pins":{"tpm2":{"pcr_ids":"7"},"tang":[{"url":"http://<tang>"}]}}'` |
| Laptop | TPM2 and a PIN, because the whole device can be stolen | `sudo systemd-cryptenroll --tpm2-device=auto --tpm2-pcrs=7 --tpm2-with-pin=yes "$DEV"` |
| VPS | Tang on the home network, reached over Tailscale; the VPS's TPM belongs to the hoster | `clevis luks bind -d "$DEV" tang '{"url":"http://<tang>.<tailnet>"}'` |

- PCR 7 binds the key to the Secure Boot state, so Secure Boot must be on:
  without it anyone could boot their own system and have the TPM hand over
  the key.
- Clevis replaces the `tpm2-device=auto` token in `/etc/crypttab` on the
  home server and the VPS; install `clevis-luks` and `clevis-systemd` (or
  `clevis-dracut`) for unlocking at boot. The Tang server runs on the router
  or another small always-on device at home (`tangd`, port 80 on the LAN
  only). Without it the home server waits at boot for Tang or the recovery
  key.

### Swap

Either no disk swap (zram only, which stays in memory), or swap with a fresh
random key on every boot. In `/etc/crypttab`:

```text
swap  /dev/<swap partition>  /dev/urandom  swap,cipher=aes-xts-plain64,size=512
```

and in `/etc/fstab`: `/dev/mapper/swap  none  swap  sw  0 0`. A random-key swap
rules out hibernation.

### Checks

```bash
findmnt -no SOURCE /var/lib/docker /var/lib/containerd   # /dev/mapper/titan-crypt[...]
lsblk -o NAME,TYPE,MOUNTPOINTS                           # titan-crypt is of TYPE crypt
swapon --show                                            # zram or /dev/dm-*, nothing plain
sudo systemd-cryptenroll "$DEV"                          # recovery and tpm2 (or clevis) slots
docker info -f '{{.DockerRootDir}}'                      # /var/lib/docker
```

Reboot once and check that the volume unlocks and TITAN comes back.

## macOS: FileVault

Docker Desktop keeps its whole disk image in the home folder
(`~/Library/Containers/com.docker.docker/`), which FileVault encrypts.

```bash
sudo fdesetup enable      # if it is off; note the recovery key
fdesetup status           # "FileVault is On."
```

- In Docker Desktop, Settings → Resources → Advanced → Disk image location
  stays on the internal disk, never on an unencrypted external one.
- After a reboot TITAN starts only once someone logs in.
  `sudo fdesetup authrestart` lets one planned restart skip that.
- On a work laptop managed by an employer,
  `sudo fdesetup hasinstitutionalrecoverykey` says whether the employer's IT
  holds a key that decrypts the disk, and with it the TITAN replica.

## Windows: BitLocker

Docker Desktop with WSL2 keeps its data in a virtual disk under
`%LOCALAPPDATA%\Docker\wsl\` (Settings → Resources → Advanced shows the
location). The drive that holds it, and the one with the WSL2 swap file, must
be encrypted:

```powershell
manage-bde -status C:     # "Protection On", "Fully Encrypted"
```

## Backups

Backups leave the encrypted volume, so they are encrypted before they leave
the node ([ADR 0007](../adr/0007-sensitive-data-protection.md)).
