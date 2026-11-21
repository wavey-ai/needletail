/*
 * Move the wall clock of one process, for the namespace lab.
 *
 * The nodes of the lab share the clock of the host. A service that is
 * started with this library preloaded reads CLOCK_REALTIME shifted by the
 * number of seconds in the file that NEEDLETAIL_CLOCK_OFFSET_FILE names. The
 * file is read again every 50 ms, so the clock of a running service can be
 * stepped and given back. No file, or an empty one, is no shift.
 * CLOCK_MONOTONIC is not touched: timers run as before.
 *
 *   cc -O2 -fPIC -shared -o clock_shift.so clock_shift.c -ldl
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/time.h>
#include <time.h>

static int (*real_clock_gettime)(clockid_t, struct timespec *);
static const char *offset_file;
static long long offset_ns;
static long long checked_ns = -1;

static void refresh(void) {
    struct timespec now;
    if (real_clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        return;
    }
    long long now_ns = (long long)now.tv_sec * 1000000000LL + now.tv_nsec;
    if (checked_ns >= 0 && now_ns - checked_ns < 50000000LL) {
        return;
    }
    checked_ns = now_ns;
    double seconds = 0.0;
    FILE *file = fopen(offset_file, "r");
    if (file != NULL) {
        if (fscanf(file, "%lf", &seconds) != 1) {
            seconds = 0.0;
        }
        fclose(file);
    }
    offset_ns = (long long)(seconds * 1e9);
}

int clock_gettime(clockid_t clock, struct timespec *out) {
    if (real_clock_gettime == NULL) {
        real_clock_gettime = dlsym(RTLD_NEXT, "clock_gettime");
        offset_file = getenv("NEEDLETAIL_CLOCK_OFFSET_FILE");
    }
    int result = real_clock_gettime(clock, out);
    if (result != 0 || clock != CLOCK_REALTIME || offset_file == NULL) {
        return result;
    }
    refresh();
    long long ns = (long long)out->tv_sec * 1000000000LL + out->tv_nsec + offset_ns;
    out->tv_sec = ns / 1000000000LL;
    out->tv_nsec = ns % 1000000000LL;
    return 0;
}

int gettimeofday(struct timeval *out, void *zone) {
    (void)zone;
    struct timespec now;
    if (clock_gettime(CLOCK_REALTIME, &now) != 0) {
        return -1;
    }
    out->tv_sec = now.tv_sec;
    out->tv_usec = now.tv_nsec / 1000;
    return 0;
}

time_t time(time_t *out) {
    struct timespec now;
    if (clock_gettime(CLOCK_REALTIME, &now) != 0) {
        return (time_t)-1;
    }
    if (out != NULL) {
        *out = now.tv_sec;
    }
    return now.tv_sec;
}
