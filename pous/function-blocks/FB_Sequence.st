FUNCTION_BLOCK FB_Sequence
VAR_INPUT
    Running             : BOOL;
    Load_Ready          : BOOL;
    Part_Present        : BOOL;
    Press_Extended      : BOOL;
    Press_Retracted     : BOOL;
    Fault_Active        : BOOL;
END_VAR

VAR_OUTPUT
    Req_Press_Extend    : BOOL;
    Step                : INT;
    Serial              : DINT;
    Leak_Testing        : BOOL;
END_VAR

VAR
    DwellTimer          : TON;
    LeakTestStub        : TON;
END_VAR

(* Stop mid-cycle: drop requests, abort the part *)
IF NOT Running AND Step <> 0 AND Step <> 50 AND Step <> 90 THEN
    Step := 90;
END_IF;

DwellTimer(IN := (Step = 20), PT := T#500ms);
LeakTestStub(IN := (Step = 40), PT := T#3s);

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
        IF LeakTestStub.Q THEN
            Step := 50;
        END_IF;

    50: (* Unload *)
        IF NOT Part_Present THEN
            Step := 0;
        END_IF;

    90: (* Aborted: part must be removed and faults cleared *)
        IF NOT Part_Present AND NOT Fault_Active THEN
            Step := 0;
        END_IF;
END_CASE;

Req_Press_Extend := (Step = 10) OR (Step = 20);
Leak_Testing := (Step = 40);

END_FUNCTION_BLOCK