# shut-down

Portable scheduled power control for an EC2 development machine.

The AWS side starts one EC2 instance every day. The instance itself owns the
shutdown deadline, warns active terminals 30 minutes beforehand, and lets the
user delay or replace that deadline from a small CLI.

## Architecture

```text
EventBridge Scheduler (09:30 local time)
                    |
                    v
              EC2 StartInstances

EC2 systemd timer (once per minute)
  |-- 30 minutes before deadline -> write a warning to active /dev/pts terminals
  |-- devbox-power delay 2        -> move the deadline by two hours
  |-- devbox-power stop-at 22     -> use the next local 22:00 deadline
  `-- at the deadline             -> systemctl poweroff -> EC2 stopped
```

There is intentionally no AWS-side stop schedule or maximum extension time.
If the local controller fails, the safe failure mode is to leave the machine
running rather than force it off.

## Requirements

- An EBS-backed EC2 Linux instance using systemd and Python 3.10 or newer.
- `InstanceInitiatedShutdownBehavior=stop`. Otherwise an OS shutdown can
  terminate the instance instead of stopping it.
- AWS credentials capable of deploying an IAM role and an EventBridge
  Scheduler schedule.
- An IANA timezone such as `America/Los_Angeles` or `Asia/Shanghai`.

## Install the local controller

Clone the repository on the instance, then run:

```bash
sudo ./install.sh \
  --timezone America/Los_Angeles \
  --default-stop-hour 19 \
  --notify-before 30 \
  --terminal-user ubuntu
```

The installer is idempotent. It installs root-owned scripts, systemd units, a
strict sudoers entry for the public CLI, and the configuration file
`/etc/devbox-power.conf`. See `config/devbox-power.conf.example` for every
supported setting.

Test terminal delivery without changing the deadline:

```bash
devbox-power notify-test
```

## Deploy scheduled startup

Deploy the CloudFormation stack in the same Region as the EC2 instance:

```bash
aws cloudformation deploy \
  --region us-west-2 \
  --stack-name devbox-power \
  --template-file infra/cloudformation/devbox-power.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    InstanceId=i-0123456789abcdef0 \
    Timezone=America/Los_Angeles \
    StartHour=9 \
    StartMinute=30
```

This creates:

- One EventBridge Scheduler schedule with its flexible window disabled.
- One execution role that can call `ec2:StartInstances` only for the configured
  instance.

The schedule calls the EC2 API at 09:30. The operating system will become
available later, after the instance finishes booting. Schedule an earlier API
call if the machine must be ready to use at exactly 09:30.

## CLI

Show the current deadline:

```bash
devbox-power status
```

Delay the existing deadline by whole hours:

```bash
devbox-power delay 2
```

Set the next occurrence of a specific whole local hour:

```bash
devbox-power stop-at 22
devbox-power stop-at 09:00
```

If that hour has already passed today, `stop-at` uses tomorrow. Minutes other
than `:00` are rejected.

Restore the next configured default hour:

```bash
devbox-power reset
```

For example, if the default deadline is 19:00 and `delay 2` runs at 18:45, the
new deadline is 21:00. The original warning is invalidated and another warning
is sent at 20:30.

## State and concurrency

The current absolute deadline is stored in
`/var/lib/devbox-power/state.json`. A future deadline survives an OS reboot. An
expired deadline is replaced with the next default deadline during a later
boot.

All state updates use an exclusive file lock, an fsynced temporary file, and an
atomic rename. If the CLI wins the lock at the deadline, the extension is
honored. If the controller has already marked the machine as stopping, the CLI
rejects the update instead of reporting a false success.

## Terminal notifications

The controller writes directly to character devices under `/dev/pts` owned by
the configured user. This covers SSH and VS Code integrated terminals that are
often absent from `who`, where a normal `wall` broadcast can miss them.

Full-screen terminal applications may redraw over a message. The notification
also emits a terminal bell, but whether that creates an audible or visual alert
depends on the terminal settings.

## Migration

To move to another EC2 instance:

1. Clone this repository and run `install.sh` on the new machine.
2. Confirm its instance-initiated shutdown behavior is `stop`.
3. Redeploy the CloudFormation stack with the new `InstanceId`.
4. Run `devbox-power notify-test`.

The old deadline normally does not need to migrate; a new host initializes the
next default 19:00 deadline. Copy `state.json` only when an active extension
must survive the migration.

Check or set the EC2 shutdown behavior from an administrative machine:

```bash
aws ec2 describe-instance-attribute \
  --instance-id i-0123456789abcdef0 \
  --attribute instanceInitiatedShutdownBehavior

aws ec2 modify-instance-attribute \
  --instance-id i-0123456789abcdef0 \
  --instance-initiated-shutdown-behavior Value=stop
```

## Operations

Inspect the timer and logs:

```bash
systemctl status devbox-power.timer
systemctl list-timers devbox-power.timer
journalctl -u devbox-power-init.service -u devbox-power.service
```

Reinstall while replacing an old deadline with the configured default:

```bash
sudo ./install.sh --timezone America/Los_Angeles --reset-state
```

Uninstall while preserving the state directory:

```bash
sudo ./uninstall.sh
```

Remove the state as well:

```bash
sudo ./uninstall.sh --purge-state
```

## Development

Run the local validation suite:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile src/devbox_power.py
bash -n install.sh uninstall.sh src/devbox-power
aws cloudformation validate-template \
  --template-body file://infra/cloudformation/devbox-power.yaml
```
