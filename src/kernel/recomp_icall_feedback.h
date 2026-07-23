/*
 * Indirect-branch target feedback - shared constants.
 *
 * Included by both src/kernel/icall_feedback.c (which defines the array) and
 * templates/runtime/recomp_types.h (which emits the store into the ICALL
 * macros). Kept separate from recomp_types.h so the generated-code header stays
 * a template and this stays a runtime detail.
 *
 * See src/kernel/icall_feedback.c for the rationale and
 * docs/technical/ms-fusion-codegen-teardown.md for where the idea comes from.
 */

#ifndef RECOMP_ICALL_FEEDBACK_H
#define RECOMP_ICALL_FEEDBACK_H

#include <stdint.h>

/* Window covered by the observation array.
 *
 * CUSTOMIZE: must span every executable section of your XBE. The default covers
 * the whole image for a 7.7 MiB XBE (tools/disasm/config.py XBE_BASE_ADDRESS /
 * XBE_IMAGE_SIZE). Targets outside the window are ignored rather than clamped --
 * a wrong bucket is worse than a missing one, because it would seed function
 * detection at an address the title never actually branched to. */
#define RECOMP_ICALL_FB_BASE 0x00010000u
#define RECOMP_ICALL_FB_SIZE 0x00800000u  /* 8 MiB */

/* Flags OR'd into g_icall_seen[va - base]. */
#define RECOMP_ICALL_SEEN_RESOLVED   1u  /* dispatch found a translation */
#define RECOMP_ICALL_SEEN_UNRESOLVED 2u  /* dispatch failed: a real gap */

#ifdef RECOMP_ICALL_FEEDBACK

extern volatile unsigned char g_icall_seen[RECOMP_ICALL_FB_SIZE];

/**
 * Record an observed indirect-branch target.
 *
 * Inlined into the ICALL macros rather than being a function call, because it
 * sits on the hottest path in the generated code: one subtract, one compare,
 * one OR.
 */
#define RECOMP_ICALL_OBSERVE(va, flags) do { \
    uint32_t _obs_off = (uint32_t)(va) - RECOMP_ICALL_FB_BASE; \
    if (_obs_off < RECOMP_ICALL_FB_SIZE) \
        g_icall_seen[_obs_off] |= (unsigned char)(flags); \
} while (0)

/**
 * Write the observed target set to a text file.
 * Merge it into the persisted database with tools/recomp/icall_feedback.py.
 * Safe to call more than once; call it from atexit() and from your crash
 * handler, since a title that dies mid-boot is exactly the one whose targets
 * you want.
 */
void recomp_icall_feedback_dump(const char *path);

#else  /* !RECOMP_ICALL_FEEDBACK */

#define RECOMP_ICALL_OBSERVE(va, flags) ((void)0)

#endif

#endif /* RECOMP_ICALL_FEEDBACK_H */
