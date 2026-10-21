FUNCTION_BLOCK FB_Sequence
VAR_INPUT
    Running             : BOOL;
    Load_Ready          : BOOL;
    Part_Present        : BOOL;
    Press_Extended      : BOOL;
    Press_Retracted     : BOOL;
END_VAR

VAR_OUTPUT
    Req_Press_Extend    : BOOL;
    Step                : INT;
    Serial              : DINT;
    PartRemoved : BOOL;
END_VAR

VAR
    DwellTimer          : TON;
    LeakStub            : TON;
END_VAR

(* Stop mid-cycle: drop requests, abort the part *)
IF NOT Running AND Step <> 0 AND Step <> 90 THEN
    Step := 90;
END_IF;

(* Timers are called every scan so they reset when their step is left *)
DwellTimer(IN := (Step = 20), PT := T#500ms);
LeakStub(IN := (Step = 40), PT := T#3s);
PartRemoved := (Step >= 10) AND (Step <= 40) AND NOT Part_Present;

IF PartRemoved THEN
    Step := 90;
END_IF;

CASE Step OF
    0: (* IDLE *)
        IF Load_Ready THEN
            Serial := Serial + 1;
            Step := 10;
        END_IF;
    
    10: (* Press extend *)
        IF Press_Extended THEN
            Step := 20;
        END_IF;

    20: (* Press dwell *)
        IF DwellTimer.Q THEN
            Step := 30;
        END_IF;

    30: (* Press retract *)
        IF Press_Retracted THEN
            Step := 40;
        END_IF;

    40: (* Leak test placeholder *)
        IF LeakStub.Q THEN
            Step := 50;
        END_IF;

    50: (* Unload *)
        IF NOT Part_Present THEN
            Step := 0;
        END_IF;

    90: (* Aborted: part must be removed *)
        IF NOT Part_Present THEN
            Step := 0;
        END_IF;
END_CASE;

Req_Press_Extend := (Step = 10) OR (Step = 20);

END_FUNCTION_BLOCK