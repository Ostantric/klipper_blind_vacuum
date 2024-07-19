import logging
from . import bus
from struct import unpack_from

PIN_MIN_TIME = 0.100
RESEND_HOST_TIME = 0.300 + PIN_MIN_TIME
MAX_SCHEDULE_TIME = 5.0

class BlindVacuum:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name().split()[-1]
        self.motor_pin = config.get('vacuum_pump_pin')
        self.valve_open_pin = config.get('valve_open_pin')
        self.valve_close_pin = config.get('valve_close_pin')
        self.vacuum_timer = config.getfloat('vacuum_timer', 600., above=0.)
        self.pump_on_time = config.getfloat('pump_on_time', 8., above=0.) #motorized valves takes time to open  + give some time for pump to create rough vacuum
        self.valve_close_time = config.getfloat('valve_close_time', 6., above=0.) #it takes time to close the valve + run pump while valve is closing
        self.vacuum_watchdog_timer = None
        self.timer_registered = False
        self.is_watchdog_activate = False
        self.is_valve_open = False
        self.is_pump_running = False
        self.is_forced_vacuum = False
        max_mcu_duration = config.getfloat('maximum_mcu_duration', 0.,
                                            minval=0.500,
                                            maxval=MAX_SCHEDULE_TIME)
        if max_mcu_duration:
            self.resend_interval = max_mcu_duration - RESEND_HOST_TIME
        self.last_value = config.getfloat(
            'value', 0., minval=0., maxval=1)
        self.shutdown_value = config.getfloat(
            'shutdown_value', 0., minval=0., maxval=1)

        ppins = self.printer.lookup_object('pins')
        #Motor_pin setup
        self.mcu_motor_pin= ppins.setup_pin('digital_out', self.motor_pin)
        self.mcu_motor_pin.setup_max_duration(max_mcu_duration)
        self.mcu_motor_pin.setup_start_value(self.last_value, self.shutdown_value)
        #valve_pin setup
        self.mcu_valve_open_pin= ppins.setup_pin('digital_out', self.valve_open_pin)
        self.mcu_valve_open_pin.setup_max_duration(max_mcu_duration)
        self.mcu_valve_open_pin.setup_start_value(self.last_value, self.shutdown_value)

        self.mcu_valve_close_pin= ppins.setup_pin('digital_out', self.valve_close_pin)
        self.mcu_valve_close_pin.setup_max_duration(max_mcu_duration)
        self.mcu_valve_close_pin.setup_start_value(self.last_value, self.shutdown_value)

        #gcode setup
        self.cmd_ENABLE_VACUUM_help = "Enable Vacuum System"
        self.cmd_DISABLE_VACUUM_help = "Disable Vacuum System"
        self.cmd_FORCE_VACUUM_ON_help = "Force valve to open and pump to run"
        self.cmd_FORCE_VACUUM_OFF_help = "Force valve to close and pump to stop"
        self.cmd_FORCE_PUMP_ON_help = "Force pump to run"
        self.cmd_FORCE_PUMP_OFF_help = "Force pump to stop"
        self.cmd_FORCE_VALVE_OPEN_help = "Force valve to open"
        self.cmd_FORCE_VALVE_CLOSE_help = "Force valve to close"

        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_command('ENABLE_VACUUM', self.cmd_ENABLE_VACUUM,
            desc=self.cmd_ENABLE_VACUUM_help)
        self.gcode.register_command('DISABLE_VACUUM', self.cmd_DISABLE_VACUUM,
            desc=self.cmd_DISABLE_VACUUM_help)
        self.gcode.register_command('FORCE_VACUUM_ON', self.cmd_FORCE_VACUUM_ON,
            desc=self.cmd_FORCE_VACUUM_ON_help)
        self.gcode.register_command('FORCE_VACUUM_OFF', self.cmd_FORCE_VACUUM_OFF,
            desc=self.cmd_FORCE_VACUUM_OFF_help)
        self.gcode.register_command('FORCE_PUMP_ON', self.cmd_FORCE_PUMP_ON,
            desc=self.cmd_FORCE_PUMP_ON_help)
        self.gcode.register_command('FORCE_PUMP_OFF', self.cmd_FORCE_PUMP_OFF,
            desc=self.cmd_FORCE_PUMP_OFF_help)
        self.gcode.register_command('FORCE_VALVE_OPEN', self.cmd_FORCE_VALVE_OPEN,
            desc=self.cmd_FORCE_VALVE_OPEN_help)
        self.gcode.register_command('FORCE_VALVE_CLOSE', self.cmd_FORCE_VALVE_CLOSE,
            desc=self.cmd_FORCE_VALVE_CLOSE_help)

    def cmd_ENABLE_VACUUM(self,gcmd):
        self.is_watchdog_activate=True
        if not self.timer_registered:
            self.vacuum_watchdog_timer = self.reactor.register_timer(self.check_vacuum_status)
            self.timer_registered = True
            self.reactor.update_timer(self.vacuum_watchdog_timer, self.reactor.NOW)
    def cmd_DISABLE_VACUUM(self,gcmd):
        self.is_watchdog_activate=False
        if self.timer_registered:
            self.reactor.unregister_timer(self.vacuum_watchdog_timer)
            self.timer_registered=False

    def cmd_FORCE_VACUUM_ON(self,gcmd):
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.register_lookahead_callback(lambda print_time: self._turn_on(print_time))
        self.is_forced_vacuum=True

    def cmd_FORCE_VACUUM_OFF(self,gcmd):
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.register_lookahead_callback(lambda print_time: self._turn_off(print_time))
        self.is_forced_vacuum=False

    def cmd_FORCE_PUMP_ON(self,gcmd):
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.register_lookahead_callback(lambda print_time: self.mcu_motor_pin.set_digital(print_time, 1))
        self.is_pump_running = True

    def cmd_FORCE_PUMP_OFF(self,gcmd):
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.register_lookahead_callback(lambda print_time: self.mcu_motor_pin.set_digital(print_time, 0))
        self.is_pump_running = False
        

    def cmd_FORCE_VALVE_OPEN(self,gcmd):
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.register_lookahead_callback(lambda print_time: self.mcu_valve_open_pin.set_digital(print_time + 1, 1))
        toolhead.register_lookahead_callback(lambda print_time: self.mcu_valve_close_pin.set_digital(print_time, 0))
        self.is_valve_open = True

    def cmd_FORCE_VALVE_CLOSE(self,gcmd):
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.register_lookahead_callback(lambda print_time: self.mcu_valve_open_pin.set_digital(print_time, 0))
        toolhead.register_lookahead_callback(lambda print_time: self.mcu_valve_close_pin.set_digital(print_time +1, 1))
        self.is_valve_open = False

    #Turn on sequence
    def _turn_on(self, print_time):
        self.mcu_motor_pin.set_digital(print_time, 1)
        self.mcu_valve_open_pin.set_digital(print_time + 2, 1) #add 2 seconds delay
        self.mcu_valve_close_pin.set_digital(print_time, 0)
        self.is_pump_running = True
        self.is_valve_open = True
    #Turn off sequence
    def _turn_off(self,print_time):
        self.mcu_motor_pin.set_digital(print_time+self.valve_close_time, 0) #add 5(6-1 from close pin, default) seconds just in case if pump internals are leaking vacuum
        self.mcu_valve_open_pin.set_digital(print_time , 0)
        self.mcu_valve_close_pin.set_digital(print_time + 1, 1) #add 1 second
        self.is_pump_running = False
        self.is_valve_open = False

    def setup_callback(self, cb):
        self._callback = cb

    def check_vacuum_status(self,eventtime):
        if self.printer.is_shutdown():
            return self.reactor.NEVER
        if self.is_watchdog_activate:
            toolhead = self.printer.lookup_object('toolhead')
            toolhead.register_lookahead_callback(lambda print_time: self._turn_on(print_time))
            toolhead.register_lookahead_callback(lambda print_time: self._turn_off(print_time+self.pump_on_time)) #give 8(default) seconds to reach max vacuum and let valve fully open
            measured_time = self.reactor.monotonic()
            return measured_time + self.vacuum_timer
        else:
            measured_time = self.reactor.monotonic()
            return measured_time
    
    def get_status(self, eventtime):
        data = {'watchdog': self.is_watchdog_activate,
                'is_valve_open': self.is_valve_open,
                'is_pump_running': self.is_pump_running,
                'is_forced_vacuum': self.is_forced_vacuum
        }
        return data 

def load_config_prefix(config):
    return BlindVacuum(config)
