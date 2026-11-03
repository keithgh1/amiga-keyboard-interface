/* Reports PAL/NTSC as seen by Exec, graphics.library and the Agnus chip itself. */
#include <stdio.h>
#include <exec/execbase.h>
#include <graphics/gfxbase.h>
#include <proto/exec.h>
#include <proto/graphics.h>

extern struct ExecBase *SysBase;
struct GfxBase *GfxBase;

static const char *agnus_name(unsigned id)
{
    switch (id) {
    case 0x00: return "OCS PAL (8367/8371)";
    case 0x10: return "OCS NTSC (8361/8370)";
    case 0x20: return "ECS PAL (8372A)";
    case 0x30: return "ECS NTSC (8372A)";
    default:   return "unknown";
    }
}

int main(void)
{
    /* VPOSR bits 14..8: Agnus revision; bit 12 set = NTSC part. */
    unsigned vposr = *(volatile unsigned short *)0xdff004;
    unsigned id = (vposr >> 8) & 0x7f;

    printf("Exec VBlankFrequency:      %u Hz\n", SysBase->VBlankFrequency);
    printf("Exec PowerSupplyFrequency: %u Hz\n", SysBase->PowerSupplyFrequency);
    GfxBase = (struct GfxBase *)OpenLibrary("graphics.library", 0);
    if (GfxBase) {
        printf("graphics DisplayFlags:     $%04x (%s), %u rows\n", GfxBase->DisplayFlags,
               (GfxBase->DisplayFlags & PAL) ? "PAL" : (GfxBase->DisplayFlags & NTSC) ? "NTSC" : "?",
               GfxBase->NormalDisplayRows);
        CloseLibrary((struct Library *)GfxBase);
    }
    printf("Agnus VPOSR id:            $%02x = %s\n", id, agnus_name(id));
    return 0;
}
