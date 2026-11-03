; LinkAgent 1.1 - CLI receiver for 68000 / Kickstart 1.3.
; DOS calls only in task context. CIA callback buffers framed bytes.
        section code,code
start:
        movem.l d2-d7/a2-a6,-(sp)
        move.l  4.w,a6
        lea     dosname(pc),a1
        moveq   #0,d0
        jsr     -552(a6)
        move.l  d0,dosbase
        beq     exit
        move.l  d0,a6
        move.l  #conname,d1
        move.l  #1006,d2
        jsr     -30(a6)
        move.l  d0,conh
        beq     close_dos
        bsr     install
        tst.l   d0
        bne     close_con
        move.l  #m_start,msg
        move.b  #1,evkind
        bsr     report
main:
        bsr     drain
        tst.b   quitting
        bne     shutdown
        move.l  4.w,a6
        moveq   #0,d0
        move.l  #$1000,d1
        jsr     -306(a6)
        and.l   #$1000,d0
        bne.s   shutdown
        move.w  head,d0
        cmp.w   oldhead,d0
        bne.s   .progress
        addq.w  #1,stall
        cmp.w   #150,stall
        blo.s   .wait
        move.l  4.w,a6
        jsr     -120(a6)
        clr.b   mode
        jsr     -126(a6)
        tst.b   active
        beq.s   .progress
        clr.b   active
        move.l  #m_timeout,msg
        bsr     badframe
.progress:
        move.w  head,oldhead
        clr.w   stall
.wait:
        move.l  dosbase,a6
        moveq   #1,d1
        jsr     -198(a6)
        bra     main
shutdown:
        ; Leave Q's final ACK visible long enough for the HTTP capture to see it.
        tst.b   quitting
        beq.s   .cleanup
        move.l  dosbase,a6
        moveq   #100,d1
        jsr     -198(a6)
.cleanup:
        bsr     abortfile
        bsr     uninstall
close_con:
        move.l  dosbase,a6
        move.l  conh,d1
        jsr     -36(a6)
close_dos:
        move.l  4.w,a6
        move.l  dosbase,a1
        jsr     -414(a6)
exit:
        movem.l (sp)+,d2-d7/a2-a6
        moveq   #0,d0
        rts

; Rem/Add is necessary: cia.resource copies is_Data/is_Code into its table.
install:
        move.l  4.w,a6
        lea     cianame(pc),a1
        jsr     -498(a6)
        move.l  d0,ciabase
        beq     .fail
        move.l  4.w,a6
        jsr     -120(a6)
        move.l  ciabase,a6
        moveq   #3,d0
        lea     ourint,a1
        jsr     -6(a6)
        move.l  d0,oldint
        beq.s   .owned
        move.l  d0,a1
        move.l  10(a1),a0
        cmpa.w  #0,a0
        beq.s   .replace
        lea     intname(pc),a1
.name:  move.b  (a0)+,d1
        cmp.b   (a1)+,d1
        bne.s   .replace
        tst.b   d1
        bne.s   .name
        ; A second process must not chain to memory owned by the first one.
        move.l  4.w,a6
        jsr     -126(a6)
        bra.s   .fail
.replace:
        move.l  oldint,a1
        move.l  14(a1),olddata
        move.l  18(a1),oldcode
        moveq   #3,d0
        jsr     -12(a6)
        moveq   #3,d0
        lea     ourint,a1
        jsr     -6(a6)
        tst.l   d0
        beq.s   .owned
        moveq   #3,d0
        move.l  oldint,a1
        jsr     -6(a6)
        move.l  4.w,a6
        jsr     -126(a6)
.fail:  moveq   #-1,d0
        rts
.owned: move.l  4.w,a6
        jsr     -126(a6)
        moveq   #0,d0
        rts
uninstall:
        move.l  4.w,a6
        jsr     -120(a6)
        move.l  ciabase,a6
        moveq   #3,d0
        lea     ourint,a1
        jsr     -12(a6)
        tst.l   oldint
        beq     .done
        move.l  oldint,a1
        moveq   #3,d0
        jsr     -6(a6)
.done:  move.l  4.w,a6
        jsr     -126(a6)
        rts

; Preserve d2-d7/a2-a6, including CIA dispatcher's d2/a2 and a5.
kbdint:
        move.b  $bfec01,d0
        not.b   d0
        ror.b   #1,d0
        tst.b   mode
        bne.s   .decode
        cmp.b   #$70,d0
        beq.s   .decode
        tst.l   oldcode
        beq.s   .ack
        move.l  a5,-(sp)
        move.l  olddata,a1
        move.l  oldcode,a5
        jsr     (a5)
        move.l  (sp)+,a5
        rts
; Handshake: SP output mode pulls KDAT low. Decoding runs inside the pulse, so the
; next byte can follow ~50 us later instead of after a fixed 80-read busy wait.
.decode:
        bset    #6,$bfee01
        bsr     decode
        bra.s   .hold
.ack:   bset    #6,$bfee01
.hold:  moveq   #7,d0
.pulse: tst.b   $bfe001
        dbra    d0,.pulse
        bclr    #6,$bfee01
        rts

; $70 START, $71 END, $72 ESC (next byte XOR $80), $f9 resync.
; Ring tags $100=start, $200=end, $300=bad. Other words are bytes.
decode:
        cmp.b   #$f9,d0
        beq.s   .bad
        cmp.b   #2,mode
        beq.s   .escaped
        cmp.b   #$70,d0
        beq.s   .start
        tst.b   mode
        beq.s   .ret
        cmp.b   #$71,d0
        beq.s   .end
        cmp.b   #$72,d0
        beq.s   .escape
        bra.s   .byte
.escaped:
        move.b  #1,mode
        eor.b   #$80,d0
.byte:  and.w   #255,d0
        bra.s   .store
.start: move.b  #1,mode
        move.w  #$100,d0
        bra.s   .store
.end:   clr.b   mode
        move.w  #$200,d0
        bra.s   .store
.bad:   clr.b   mode
        move.w  #$300,d0
        bra.s   .store
.escape:
        move.b  #2,mode
.ret:   rts
.store: lea     ring,a0
        moveq   #0,d1
        move.w  head,d1
        add.w   d1,d1
        move.w  d0,0(a0,d1.w)
        lsr.w   #1,d1
        addq.w  #1,d1
        and.w   #8191,d1
        cmp.w   tail,d1
        beq.s   .over
        move.w  d1,head
        rts
.over:  st      overflow
        rts

drain:
        moveq   #0,d0
        move.w  tail,d0
        cmp.w   head,d0
        beq     .done
        lea     ring,a0
        add.w   d0,d0
        move.w  0(a0,d0.w),d1
        lsr.w   #1,d0
        addq.w  #1,d0
        and.w   #8191,d0
        move.w  d0,tail
        cmp.w   #$100,d1
        beq.s   .start
        cmp.w   #$200,d1
        beq.s   .end
        cmp.w   #$300,d1
        beq.s   .bad
        tst.b   active
        beq     drain
        move.l  pktlen,d0
        cmp.l   #4096,d0
        bhs.s   .bad
        lea     packet,a0
        move.b  d1,0(a0,d0.l)
        addq.l  #1,pktlen
        bra     drain
.start: st      active
        clr.l   pktlen
        clr.b   overflow
        bra     drain
.bad:   tst.b   active
        beq     drain
        clr.b   active
        move.l  #m_badframe,msg
        bsr     badframe
        bra     drain
.end:   tst.b   active
        beq     drain
        clr.b   active
        tst.b   overflow
        bne.s   .lost
        bsr     dispatch
        bra     drain
.lost:  move.l  #m_overflow,msg
        bsr     badframe
        bra     drain
.done:  rts

; zlib CRC32: a0=bytes,d0=count,d1=running CRC (initial -1). Final XOR outside.
crc:
        movem.l d2-d3,-(sp)
        tst.l   d0
        beq     .done
.byte:  moveq   #0,d2
        move.b  (a0)+,d2
        eor.l   d2,d1
        moveq   #7,d3
.bit:   lsr.l   #1,d1
        bcc.s   .next
        eor.l   #$edb88320,d1
.next:  dbra    d3,.bit
        subq.l  #1,d0
        bne.s   .byte
.done:  movem.l (sp)+,d2-d3
        rts
; Read an unaligned big-endian long. 68000 forbids odd word/long reads.
getlong:
        moveq   #0,d0
        moveq   #3,d1
.loop:  lsl.l   #8,d0
        move.b  (a0)+,d0
        dbra    d1,.loop
        rts

dispatch:
        move.l  #m_badframe,msg
        move.l  pktlen,d0
        cmp.l   #6,d0
        blo     badframe
        subq.l  #4,d0
        move.l  d0,bodylen
        lea     packet,a0
        moveq   #-1,d1
        bsr     crc
        not.l   d1
        move.l  d1,framecrc
        bsr     getlong
        cmp.l   framecrc,d0
        bne     badframe
        move.b  packet,pktseq
        move.b  packet+1,cmdstr
        clr.b   evkind
        tst.b   cached
        beq.s   .new
        move.b  pktseq,d0
        cmp.b   lastseq,d0
        bne.s   .new
        move.l  framecrc,d0
        cmp.l   lastcrc,d0
        bne.s   .new
        move.b  lastresult,result
        addq.l  #1,nrepeat
        move.l  #m_repeat,msg
        bra     report
.new:   move.b  #1,result
        addq.l  #1,npackets
        move.l  #m_badreq,msg
        lea     packet+2,a0
        move.l  bodylen,d7
        subq.l  #2,d7
        move.b  packet+1,d0
        cmp.b   #'P',d0
        beq     ping
        cmp.b   #'B',d0
        beq     beginfile
        cmp.b   #'D',d0
        beq     datafile
        cmp.b   #'E',d0
        beq     endfile
        cmp.b   #'A',d0
        beq     abortcmd
        cmp.b   #'Q',d0
        bne.s   finish
        tst.l   d7
        bne.s   finish
        st      quitting
        move.l  #m_quit,msg
        bra.s   success
ping:   tst.l   d7
        bne.s   finish
        move.l  #m_ping,msg
success:
        clr.b   result
finish: st      cached
        move.b  pktseq,lastseq
        move.b  result,lastresult
        move.l  framecrc,lastcrc
        bra     report

; A frame that never became a packet: no ACK (the PC resends), but say so on screen.
; msg is set by the caller.
badframe:
        addq.l  #1,nbad
        move.b  #1,evkind
        bra     drawstatus

; B: size.l crc32.l path (1..180 bytes). Existing files are protected.
beginfile:
        cmp.l   #9,d7
        blo     finish
        cmp.l   #188,d7
        bhi     finish
        move.l  #m_busy,msg
        tst.l   fileh
        bne     finish
        move.l  #m_badreq,msg
        bsr     getlong
        move.l  d0,expectedsize
        bmi     finish
        bsr     getlong
        move.l  d0,expectedcrc
        subq.l  #8,d7
        clr.l   received
        move.b  #3,state
        lea     filename,a1
        lea     tempname,a2
        subq.w  #1,d7
.copy:  move.b  (a0)+,d0
        beq     .badname
        cmp.b   #32,d0
        blo     .badname
        move.b  d0,(a1)+
        move.b  d0,(a2)+
        dbra    d7,.copy
        clr.b   (a1)
        move.b  #'.',(a2)+
        move.b  #'p',(a2)+
        move.b  #'a',(a2)+
        move.b  #'r',(a2)+
        move.b  #'t',(a2)+
        clr.b   (a2)
        move.l  dosbase,a6
        move.l  #m_exists,msg
        move.l  #filename,d1
        bsr     exists
        tst.l   d0
        bne     fileerror
        move.l  #m_partexists,msg
        move.l  #tempname,d1
        bsr     exists
        tst.l   d0
        bne     fileerror
        move.l  #m_create,msg
        move.l  #tempname,d1
        move.l  #1006,d2
        jsr     -30(a6)
        move.l  d0,fileh
        beq     fileerror
        move.l  #-1,filecrc
        move.b  #1,state
        move.l  #m_begin,msg
        bra     success
.badname:
        clr.b   (a1)
        bra     finish
exists:
        moveq   #-2,d2
        jsr     -84(a6)
        tst.l   d0
        beq.s   .no
        move.l  d0,d1
        jsr     -90(a6)
        moveq   #1,d0
.no:    rts

; D: expected offset.l plus data. Retries of the last frame have no side effects.
datafile:
        move.l  #m_noopen,msg
        tst.l   fileh
        beq     finish
        move.l  #m_badreq,msg
        cmp.l   #4,d7
        blo     finish
        bsr     getlong
        move.l  #m_offset,msg
        cmp.l   received,d0
        bne     finish
        subq.l  #4,d7
        move.l  d0,d1
        add.l   d7,d1
        move.l  #m_toobig,msg
        cmp.l   expectedsize,d1
        bhi     finish
        move.l  a0,a3
        move.l  dosbase,a6
        move.l  fileh,d1
        move.l  a3,d2
        move.l  d7,d3
        jsr     -48(a6)
        cmp.l   d7,d0
        bne.s   .writefail
        move.l  a3,a0
        move.l  d7,d0
        move.l  filecrc,d1
        bsr     crc
        move.l  d1,filecrc
        add.l   d7,received
        move.l  #m_data,msg
        bra     success
.writefail:
        bsr     abortfile
        move.b  #3,state
        move.l  #m_writefail,msg
        bra     fileerror
endfile:
        tst.l   d7
        bne     finish
        move.l  #m_noopen,msg
        tst.l   fileh
        beq     finish
        move.l  #m_short,msg
        move.l  received,d0
        cmp.l   expectedsize,d0
        bne     finish
        move.l  #m_crcbad,msg
        move.l  filecrc,d0
        not.l   d0
        cmp.l   expectedcrc,d0
        bne     finish
        move.l  dosbase,a6
        move.l  fileh,d1
        clr.l   fileh
        ; KS 1.3 Close() has no return value; d0 is undefined.
        jsr     -36(a6)
        move.b  #3,state
        move.l  #m_exists,msg
        move.l  #filename,d1
        bsr     exists
        tst.l   d0
        bne.s   fileerror
        move.l  #m_rename,msg
        move.l  #tempname,d1
        move.l  #filename,d2
        jsr     -78(a6)
        tst.l   d0
        beq.s   fileerror
        move.b  #2,state
        addq.l  #1,nfiles
        move.l  expectedsize,d0
        add.l   d0,nbytes
        move.l  #m_saved,msg
        bra     success
abortcmd:
        tst.l   d7
        bne     finish
        move.l  #m_noabort,msg
        tst.l   fileh
        beq.s   .idle
        move.b  #3,state
        move.l  #m_abort,msg
.idle:  bsr     abortfile
        bra     success
fileerror:
        move.b  #2,result
        bra     finish
abortfile:
        tst.l   fileh
        beq     .done
        move.l  dosbase,a6
        move.l  fileh,d1
        clr.l   fileh
        jsr     -36(a6)
        move.l  #tempname,d1
        jsr     -72(a6)
.done:  rts

; 40-bit screen ACK: A5 pktseq result (A5 XOR pktseq XOR result) 5A.
; Inverse space=1; normal space=0. 40 cells, MSB first.
; Rewrites only the stripe row, one attribute change per run of equal bits, and
; writes it before the status rows so the PC sees the ACK as early as possible.
report:
        movem.l d2-d7/a2-a3,-(sp)
        lea     response,a2
        lea     row2(pc),a0
.prefix:
        move.b  (a0)+,d0
        beq.s   .bits
        move.b  d0,(a2)+
        bra.s   .prefix
.bits:  move.b  #$a5,barcode
        move.b  pktseq,barcode+1
        move.b  result,barcode+2
        move.b  #$a5,d0
        move.b  pktseq,d1
        eor.b   d1,d0
        move.b  result,d1
        eor.b   d1,d0
        move.b  d0,barcode+3
        move.b  #$5a,barcode+4
        lea     barcode,a3
        moveq   #0,d3
        moveq   #4,d5
.byte:  move.b  (a3)+,d4
        moveq   #7,d6
.bit:   move.b  #'0',d0
        lsl.b   #1,d4
        bcc.s   .normal
        move.b  #'7',d0
.normal:
        cmp.b   d0,d3
        beq.s   .space
        move.b  d0,d3
        move.b  #27,(a2)+
        move.b  #'[',(a2)+
        move.b  d0,(a2)+
        move.b  #'m',(a2)+
.space: move.b  #' ',(a2)+
        dbra    d6,.bit
        dbra    d5,.byte
        lea     plain(pc),a0
.suffix:
        move.b  (a0)+,d0
        beq.s   .write
        move.b  d0,(a2)+
        bra.s   .suffix
.write: move.l  a2,d3
        sub.l   #response,d3
        move.l  #response,d2
        move.l  conh,d1
        move.l  dosbase,a6
        jsr     -48(a6)
        bsr     drawstatus
        movem.l (sp)+,d2-d7/a2-a3
        rts

; Rows 1, 3, 4 and 5: state, file, last event, counters. Lines stay under 76
; columns: a wrap on row 5 would scroll the window and strand an old stripe.
drawstatus:
        movem.l d2-d7/a2-a6,-(sp)
        lea     line,a3
        lea     s_row1(pc),a0
        bsr     puts
        lea     args,a1
        move.l  #s_idle,(a1)
        tst.l   fileh
        beq.s   .row1
        move.l  #s_recv,(a1)
.row1:  lea     f_state(pc),a0
        bsr     format
        lea     s_row3(pc),a0
        bsr     puts
        bsr     shortname
        lea     args,a1
        move.l  #dispname,(a1)
        lea     f_none(pc),a0
        moveq   #0,d0
        move.b  state,d0
        beq.s   .row3
        subq.b  #1,d0
        bne.s   .notrecv
        move.l  received,4(a1)
        move.l  expectedsize,8(a1)
        move.l  received,d0
        move.l  expectedsize,d1
        bsr     percent
        move.l  d0,12(a1)
        move.l  #s_pct,16(a1)
        move.l  expectedcrc,20(a1)
        lea     f_recv(pc),a0
        bra.s   .row3
.notrecv:
        subq.b  #1,d0
        bne.s   .failed
        move.l  expectedsize,4(a1)
        move.l  expectedcrc,8(a1)
        lea     f_saved(pc),a0
        bra.s   .row3
.failed:
        move.l  received,4(a1)
        move.l  expectedsize,8(a1)
        lea     f_failed(pc),a0
.row3:  bsr     format
        lea     s_row4(pc),a0
        bsr     puts
        lea     args,a1
        move.l  msg,(a1)
        lea     f_event(pc),a0
        tst.b   evkind
        bne.s   .row4
        moveq   #0,d0
        move.b  pktseq,d0
        move.l  d0,(a1)
        move.l  #cmdstr,4(a1)
        move.b  result,d0
        lsl.w   #2,d0
        lea     resnames(pc),a0
        move.l  0(a0,d0.w),8(a1)
        move.l  msg,12(a1)
        lea     f_packet(pc),a0
.row4:  bsr     format
        lea     s_row5(pc),a0
        bsr     puts
        lea     args,a1
        move.l  npackets,(a1)
        move.l  nrepeat,4(a1)
        move.l  nbad,8(a1)
        move.l  nfiles,12(a1)
        move.l  nbytes,16(a1)
        lea     f_counts(pc),a0
        bsr     format
        move.l  a3,d3
        sub.l   #line,d3
        move.l  #line,d2
        move.l  conh,d1
        move.l  dosbase,a6
        jsr     -48(a6)
        movem.l (sp)+,d2-d7/a2-a6
        rts

; exec RawDoFmt(a0=format, a1=args) appended at a3, then erase to end of line.
; a3 is rescanned for the NUL rather than trusting its value after the call.
format: move.l  a3,d7
        lea     putch(pc),a2
        move.l  4.w,a6
        jsr     -522(a6)
        move.l  d7,a3
.end:   tst.b   (a3)+
        bne.s   .end
        subq.l  #1,a3
        lea     s_eol(pc),a0
puts:   move.b  (a0)+,d0
        beq.s   .done
        move.b  d0,(a3)+
        bra.s   puts
.done:  rts
putch:  move.b  d0,(a3)+
        rts

; dispname = filename, or "..." plus its last 23 characters.
shortname:
        lea     filename,a0
        move.l  a0,a1
.len:   tst.b   (a1)+
        bne.s   .len
        subq.l  #1,a1
        move.l  a1,d0
        sub.l   a0,d0
        lea     dispname,a2
        cmp.l   #26,d0
        bls.s   .copy
        move.b  #'.',(a2)+
        move.b  #'.',(a2)+
        move.b  #'.',(a2)+
        lea     -23(a1),a0
.copy:  move.b  (a0)+,(a2)+
        bne.s   .copy
        rts

; d0 = d0 * 100 / d1 for 0 <= d0 <= d1, using only 16-bit mulu/divu.
percent:
        tst.l   d1
        bne.s   .scale
        moveq   #100,d0
        rts
.scale: cmp.l   #$ffff,d1
        bls.s   .div
        lsr.l   #1,d1
        lsr.l   #1,d0
        bra.s   .scale
.div:   mulu    #100,d0
        divu    d1,d0
        and.l   #$ffff,d0
        rts

dosname: dc.b 'dos.library',0
cianame: dc.b 'ciaa.resource',0
; The interrupt name identifies any running LinkAgent (duplicate check); keep it.
intname: dc.b 'LinkAgent SP 1.0',0
; Five text lines only, leaving room for a Shell below.
conname: dc.b 'CON:0/12/640/56/LinkAgent 1.1 - keep this window visible',0
row2:   dc.b 27,'[2;1H',0
plain:  dc.b 27,'[0m',0
s_row1: dc.b 27,'[1;1H',27,'[0m',0
s_row3: dc.b 27,'[3;1H',0
s_row4: dc.b 27,'[4;1H',0
s_row5: dc.b 27,'[5;1H',0
s_eol:  dc.b 27,'[K',0
f_state: dc.b 'LinkAgent 1.1   %s',0
s_idle: dc.b 'IDLE',0
s_recv: dc.b 'RECEIVING',0
f_none: dc.b 'No file received yet.',0
f_recv: dc.b 'Receiving %s  %ld/%ld (%ld%s)  CRC32 %08lx',0
f_saved: dc.b 'Saved %s  %ld bytes  CRC32 %08lx',0
f_failed: dc.b 'Failed %s  at %ld/%ld bytes',0
s_pct:  dc.b '%',0
f_packet: dc.b 'Packet #%ld %s: %s - %s',0
f_event: dc.b '%s',0
f_counts: dc.b 'Packets %ld  repeats %ld  bad frames %ld  saved %ld files, %ld bytes',0
s_ok:   dc.b 'OK',0
s_rejected: dc.b 'REJECTED',0
s_diskerr: dc.b 'DISK ERROR',0
m_start: dc.b 'Ready. Waiting for amipush on the PC.',0
m_ping: dc.b 'ping',0
m_badreq: dc.b 'malformed or unknown request',0
m_busy: dc.b 'another transfer is still open',0
m_exists: dc.b 'destination already exists',0
m_partexists: dc.b 'old .part file exists, delete it',0
m_create: dc.b 'cannot create .part (path/disk?)',0
m_begin: dc.b 'transfer started',0
m_noopen: dc.b 'no transfer is open',0
m_offset: dc.b 'unexpected offset (block lost?)',0
m_toobig: dc.b 'data beyond the declared size',0
m_data: dc.b 'block written',0
m_writefail: dc.b 'disk write failed, transfer aborted',0
m_short: dc.b 'size mismatch, not saved',0
m_crcbad: dc.b 'file CRC32 mismatch, not saved',0
m_rename: dc.b 'rename to final name failed',0
m_saved: dc.b 'saved, size and CRC32 verified',0
m_abort: dc.b 'transfer aborted, .part deleted',0
m_noabort: dc.b 'abort: no transfer was open',0
m_quit: dc.b 'quitting, keyboard restored',0
m_repeat: dc.b 'repeated packet, result resent',0
m_badframe: dc.b 'Corrupt frame ignored; the PC will resend.',0
m_overflow: dc.b 'Frame lost: receive buffer overflow.',0
m_timeout: dc.b 'Incomplete frame timed out.',0
        even
resnames: dc.l s_ok,s_rejected,s_diskerr
        even
ourint: dc.l 0,0
        dc.b 2,0
        dc.l intname,0,kbdint

        section data,bss
dosbase: ds.l 1
conh:   ds.l 1
ciabase: ds.l 1
oldint: ds.l 1
olddata: ds.l 1
oldcode: ds.l 1
fileh:  ds.l 1
expectedsize: ds.l 1
expectedcrc: ds.l 1
received: ds.l 1
filecrc: ds.l 1
pktlen: ds.l 1
bodylen: ds.l 1
framecrc: ds.l 1
lastcrc: ds.l 1
head:   ds.w 1
tail:   ds.w 1
oldhead: ds.w 1
stall:  ds.w 1
mode:   ds.b 1
active: ds.b 1
overflow: ds.b 1
quitting: ds.b 1
pktseq:    ds.b 1
result: ds.b 1
cached: ds.b 1
lastseq: ds.b 1
lastresult: ds.b 1
state:  ds.b 1          ; 0 none, 1 receiving, 2 saved, 3 failed
evkind: ds.b 1          ; 0 packet result, 1 frame-level event
cmdstr: ds.b 2
        even
msg:    ds.l 1
npackets: ds.l 1
nrepeat: ds.l 1
nbad:   ds.l 1
nfiles: ds.l 1
nbytes: ds.l 1
args:   ds.l 8
dispname: ds.b 32
line:   ds.b 512
barcode: ds.b 5
        even
filename: ds.b 192
tempname: ds.b 192
response: ds.b 512
packet: ds.b 4096
ring:   ds.w 8192




