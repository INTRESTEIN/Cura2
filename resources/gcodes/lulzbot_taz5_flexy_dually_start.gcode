;{material_print_temperature}{material_bed_temperature}
M140 S{material_bed_temperature}    ; start bed heating up
G21        ;metric values
G90        ;absolute positioning
M107       ;start with the fan off
G28 X0 Y0  ;move X/Y to min endstops
G28 Z0     ;move Z to min endstops
G1 Z15.0 F{speed_travel} ;move the platform down 15mm
M117 Heating...                     ; progress indicator message on LCD
M104 S{material_print_temperature_0} T0 ; set extruder temp
M104 S{material_print_temperature_1} T1; set extruder temp
M109 R{material_print_temperature_0} T0 ; set extruder temp and wait
M109 R{material_print_temperature_1} T1; set extruder temp and wait
M190 S{material_bed_temperature_layer_0}    ; wait for bed to reach printing temp
T1                      ;Switch to the 2nd extruder
G92 E0                  ;zero the extruded length
G1 F100 E10             ;extrude 10mm of feed stock
G92 E0                  ;zero the extruded length again
G1 F200 E-{retraction_amount}
T0                      ;Switch to the first extruder
G92 E0                  ;zero the extruded length
G1 F100 E10             ;extrude 10mm of feed stock
G92 E0                  ;zero the extruded length again
G1 F{speed_travel}
;Put printing message on LCD screen
M117 Printing...