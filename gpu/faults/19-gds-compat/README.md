# GPUDirect Storage in compatibility mode (nvidia_fs not loaded)

Ticket: Checkpoint saves from h100-03 take about 3x longer than from the other nodes. The storage (VAST) dashboards look normal.

<details><summary>Answer</summary>

h100-03 booted a newer kernel (6.8.0-90-generic, locked: 6.8.0-87-generic) and the nvidia_fs module wasn't rebuilt for it, so it isn't loaded. cuFile silently falls back to compatibility mode, bouncing data through CPU memory: gdscheck -p shows nvidia_fs N/A, lsmod has no nvidia_fs, and gdsio reads ~1/3 of normal. Action: boot the locked kernel (or install nvidia-fs DKMS for this one), modprobe nvidia_fs, rerun gdsio. Pin the kernel in the image and add the module check to validation.

</details>
