/* SPDX-License-Identifier: MIT */
/* Copyright (c) 2026 Keith Monahan */
/* vbcc +a500 smoke test: C library output plus Exec calls under KS 1.3. */
#include <stdio.h>
#include <exec/execbase.h>
#include <exec/memory.h>
#include <proto/exec.h>

extern struct ExecBase *SysBase;

int main(void)
{
    printf("Hello from vbcc on the A500!\n");
    printf("Kickstart %u.%u, chip free %lu, fast free %lu\n",
           SysBase->LibNode.lib_Version, SysBase->LibNode.lib_Revision,
           AvailMem(MEMF_CHIP), AvailMem(MEMF_FAST));
    return 0;
}
