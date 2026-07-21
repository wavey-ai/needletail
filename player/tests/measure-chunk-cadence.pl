use strict;
use warnings;
use Time::HiRes qw(time);

binmode STDIN;
my $last = time;
my $start = $last;
my $maximum = 0;
my $maximum_at = 0;
my $chunks = 0;

while (read(STDIN, my $buffer, 1316) == 1316) {
    my $now = time;
    my $gap = $now - $last;
    if ($gap > $maximum) {
        $maximum = $gap;
        $maximum_at = $now - $start;
    }
    if ($gap > 0.1) {
        printf "slow_gap_ms=%.1f at_wall_s=%.3f\n", $gap * 1000, $now - $start;
    }
    $last = $now;
    $chunks += 1;
}

printf "chunks=%d max_gap_ms=%.1f at_wall_s=%.3f total_wall_s=%.3f\n",
    $chunks, $maximum * 1000, $maximum_at, time - $start;
