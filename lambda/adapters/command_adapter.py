import subprocess


def read_metric(config):
    """Read a numeric metric by running a shell command.

    WARNING: The script value is executed as-is via shell=True.
    Only use trusted input — the value comes from the Terraform
    CONFIG env var, not from runtime user input.

    Config keys:
        script: Shell command to execute (stdout must be a number)
    """
    result = subprocess.run(
        config["script"],
        shell=True,
        capture_output=True,
        text=True,
        timeout=25,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Command exited with code {result.returncode}: {result.stderr.strip()}"
        )

    output = result.stdout.strip()
    if not output:
        raise ValueError("Command produced no output")

    return float(output)
