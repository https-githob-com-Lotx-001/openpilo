from cereal import car
from openpilot.common.conversions import Conversions as CV
from openpilot.common.numpy_fast import clip
from openpilot.common.realtime import DT_CTRL
from opendbc.can.packer import CANPacker
from openpilot.selfdrive.car import apply_driver_steer_torque_limits, common_fault_avoidance, apply_std_steer_angle_limits
from openpilot.selfdrive.car.hyundai import hyundaicanfd, hyundaican
from openpilot.selfdrive.car.hyundai.hyundaicanfd import CanBus
from openpilot.selfdrive.car.hyundai.values import HyundaiFlags, Buttons, CarControllerParams, CANFD_CAR, CAR, CAN_GEARS
from openpilot.selfdrive.car.interfaces import CarControllerBase, ACCEL_MIN, ACCEL_MAX

from openpilot.selfdrive.controls.neokii.navi_controller import SpeedLimiter

VisualAlert = car.CarControl.HUDControl.VisualAlert
LongCtrlState = car.CarControl.Actuators.LongControlState

# EPS faults if you apply torque while the steering angle is above 90 degrees for more than 1 second
# All slightly below EPS thresholds to avoid fault
MAX_ANGLE = 85
MAX_ANGLE_FRAMES = 89
MAX_ANGLE_CONSECUTIVE_FRAMES = 2


def process_hud_alert(enabled, fingerprint, hud_control):
  sys_warning = (hud_control.visualAlert in (VisualAlert.steerRequired, VisualAlert.ldw))

  # initialize to no line visible
  # TODO: this is not accurate for all cars
  sys_state = 1
  if hud_control.leftLaneVisible and hud_control.rightLaneVisible or sys_warning:  # HUD alert only display when LKAS status is active
    sys_state = 3 if enabled or sys_warning else 4
  elif hud_control.leftLaneVisible:
    sys_state = 5
  elif hud_control.rightLaneVisible:
    sys_state = 6

  # initialize to no warnings
  left_lane_warning = 0
  right_lane_warning = 0
  if hud_control.leftLaneDepart:
    left_lane_warning = 1 if fingerprint in (CAR.GENESIS_G90, CAR.GENESIS_G80) else 2
  if hud_control.rightLaneDepart:
    right_lane_warning = 1 if fingerprint in (CAR.GENESIS_G90, CAR.GENESIS_G80) else 2

  return sys_warning, sys_state, left_lane_warning, right_lane_warning


class CarController(CarControllerBase):
  def __init__(self, dbc_name, CP, VM):
    self.CP = CP
    self.CAN = CanBus(CP)
    self.params = CarControllerParams(CP)
    self.packer = CANPacker(dbc_name)
    self.angle_limit_counter = 0
    self.frame = 0

    self.accel_last = 0
    self.apply_steer_last = 0
    self.car_fingerprint = CP.carFingerprint
    self.last_button_frame = 0

    self.apply_angle_last = 0
    self.lkas_max_torque = 0

    self.turning_signal_timer = 0
    self.turning_indicator_alert = False

  def update(self, CC, CS, now_nanos):
    actuators = CC.actuators
    hud_control = CC.hudControl

    # steering torque
    new_steer = int(round(actuators.steer * self.params.STEER_MAX))
    apply_steer = apply_driver_steer_torque_limits(new_steer, self.apply_steer_last, CS.out.steeringTorque, self.params)

    # >90 degree steering fault prevention
    self.angle_limit_counter, apply_steer_req = common_fault_avoidance(abs(CS.out.steeringAngleDeg) >= MAX_ANGLE, CC.latActive,
                                                                       self.angle_limit_counter, MAX_ANGLE_FRAMES,
                                                                       MAX_ANGLE_CONSECUTIVE_FRAMES)

    apply_angle = apply_std_steer_angle_limits(actuators.steeringAngleDeg, self.apply_angle_last, CS.out.vEgoRaw, self.params)

    # Figure out torque value.  On Stock when LKAS is active, this is variable,
    # but 0 when LKAS is not actively steering, so because we're "tricking" ADAS
    # into thinking LKAS is always active, we need to make sure we're applying
    # torque when the driver is not actively steering. The default value chosen
    # here is based on observations of the stock LKAS system when it's engaged
    # CS.out.steeringPressed and steeringTorque are based on the
    # STEERING_COL_TORQUE value
    MAX_TORQUE = 200
    if not bool(CS.out.steeringPressed):
      # If steering is not pressed, use max torque (TODO: need to find this value)
      self.lkas_max_torque = MAX_TORQUE
    else:
      # Steering torque seems to be a different scale than applied torque, so we
      # calculate a percentage based on observed "max" values (~|1200| based on
      # MDPS STEERING_COL_TORQUE) and then apply that percentage to our normal
      # max torque, use min to clamp to 100%
      driver_applied_torque_pct = min(abs(CS.out.steeringTorque) / 1200.0, 1.0)
      # Use max(0, ...) to avoid negative torque in case the
      self.lkas_max_torque = MAX_TORQUE - (driver_applied_torque_pct * MAX_TORQUE)

    # Disable steering while turning blinker on and speed below 60 kph
    if CS.out.leftBlinker or CS.out.rightBlinker:
      self.turning_signal_timer = 0.5 / DT_CTRL  # Disable for 0.5 Seconds after blinker turned off
    if self.turning_indicator_alert: # set and clear by interface
      CC.latActive = False
    if self.turning_signal_timer > 0:
      self.turning_signal_timer -= 1

    if not CC.latActive:
      apply_angle = CS.out.steeringAngleDeg
      apply_steer = 0
      self.lkas_max_torque = 0

    self.apply_angle_last = apply_angle

    # Hold torque with induced temporary fault when cutting the actuation bit
    torque_fault = CC.latActive and not apply_steer_req

    self.apply_steer_last = apply_steer

    # accel + longitudinal
    accel = clip(actuators.accel, ACCEL_MIN, ACCEL_MAX)
    stopping = actuators.longControlState == LongCtrlState.stopping
    set_speed_in_units = hud_control.setSpeed * (CV.MS_TO_KPH if CS.is_metric else CV.MS_TO_MPH)

    # HUD messages
    sys_warning, sys_state, left_lane_warning, right_lane_warning = process_hud_alert(CC.enabled, self.car_fingerprint, hud_control)

    can_sends = []

    # *** common hyundai stuff ***

    # CAN-FD platforms
    if self.CP.carFingerprint in CANFD_CAR:
      hda2 = self.CP.flags & HyundaiFlags.CANFD_HDA2
      hda2_long = hda2 and self.CP.openpilotLongitudinalControl

      # tester present - w/ no response (keeps relevant ECU disabled)
      if self.frame % 100 == 0 and not (self.CP.flags & HyundaiFlags.CANFD_CAMERA_SCC.value) and self.CP.openpilotLongitudinalControl:
        # for longitudinal control, either radar or ADAS driving ECU
        addr, bus = 0x7d0, 0
        if self.CP.flags & HyundaiFlags.CANFD_HDA2.value:
          addr, bus = 0x730, self.CAN.ECAN
        can_sends.append([addr, 0, b"\x02\x3E\x80\x00\x00\x00\x00\x00", bus])

        # for blinkers
        if self.CP.flags & HyundaiFlags.ENABLE_BLINKERS:
          can_sends.append([0x7b1, 0, b"\x02\x3E\x80\x00\x00\x00\x00\x00", self.CAN.ECAN])

      # steering control
      if self.CP.flags & HyundaiFlags.STEER_ANGLE:
        can_sends.extend(hyundaicanfd.create_steering_messages_angle(self.packer, self.CP, self.CAN, CC.enabled,
                                                                     apply_steer_req, CS.out.steeringPressed,
                                                                     apply_steer, apply_angle, self.lkas_max_torque))
      else:
        can_sends.extend(hyundaicanfd.create_steering_messages(self.packer, self.CP, self.CAN, CC.enabled, apply_steer_req, apply_steer))

      # prevent LFA from activating on HDA2 by sending "no lane lines detected" to ADAS ECU
      if self.frame % 5 == 0 and hda2:
        can_sends.append(hyundaicanfd.create_suppress_lfa(self.packer, self.CAN, CS.hda2_lfa_block_msg,
                                                          self.CP.flags & HyundaiFlags.CANFD_HDA2_ALT_STEERING))

      # LFA and HDA icons
      if self.frame % 5 == 0 and (not hda2 or hda2_long):
        can_sends.append(hyundaicanfd.create_lfahda_cluster(self.packer, self.CAN, CC.enabled))

      # blinkers
      if hda2 and self.CP.flags & HyundaiFlags.ENABLE_BLINKERS:
        can_sends.extend(hyundaicanfd.create_spas_messages(self.packer, self.CAN, self.frame, CC.leftBlinker, CC.rightBlinker))

      if self.CP.openpilotLongitudinalControl:
        if hda2:
          can_sends.extend(hyundaicanfd.create_adrv_messages(self.packer, self.CAN, self.frame))
        if self.frame % 2 == 0:
          can_sends.append(hyundaicanfd.create_acc_control(self.packer, self.CAN, CC.enabled, self.accel_last, accel, stopping, CC.cruiseControl.override,
                                                           set_speed_in_units, hud_control))
          self.accel_last = accel
      else:
        # button presses
        can_sends.extend(self.create_button_messages(CC, CS, use_clu11=False))
    else:
      send_lfa = self.CP.flags & HyundaiFlags.SEND_LFA.value
      use_fca = self.CP.flags & HyundaiFlags.USE_FCA.value

      can_sends.append(hyundaican.create_lkas11(self.packer, self.frame, self.CP, apply_steer, apply_steer_req, torque_fault, sys_warning, sys_state, CC.enabled,
                                                hud_control.leftLaneVisible, hud_control.rightLaneVisible, left_lane_warning, right_lane_warning,
                                                send_lfa, CS.lkas11))

      if not self.CP.openpilotLongitudinalControl:
        can_sends.extend(self.create_button_messages(CC, CS, use_clu11=True))

      if self.frame % 2 == 0 and self.CP.openpilotLongitudinalControl:
        # TODO: unclear if this is needed
        jerk = 3.0 if actuators.longControlState == LongCtrlState.pid else 1.0
        can_sends.extend(hyundaican.create_scc_commands(self.packer, accel, jerk, int(self.frame / 2),
                                                        hud_control, set_speed_in_units, stopping, CC, CS, use_fca))

      # 20 Hz LFA MFA message
      if self.frame % 5 == 0 and send_lfa:
        can_sends.append(hyundaican.create_lfahda_mfc(self.packer, CC.enabled, SpeedLimiter.instance().get_active()))

      # 5 Hz ACC options
      if self.frame % 20 == 0 and self.CP.openpilotLongitudinalControl and self.CP.sccBus == 0:
        can_sends.extend(hyundaican.create_acc_opt(self.packer, use_fca))
      elif CS.scc13 is not None:
        can_sends.append(hyundaican.create_acc_opt_none(self.packer, CS))

      if self.CP.carFingerprint in CAN_GEARS["send_mdps12"]:  # send mdps12 to LKAS to prevent LKAS error
        can_sends.append(hyundaican.create_mdps12(self.packer, self.frame, CS.mdps12))

      # 2 Hz front radar options
      if self.frame % 50 == 0 and self.CP.openpilotLongitudinalControl and self.CP.sccBus == 0:
        can_sends.append(hyundaican.create_frt_radar_opt(self.packer))

      # car signal status
      if self.frame % 1000 == 0:
        print(f'scc11 = {bool(CS.scc11)}  scc12 = {bool(CS.scc12)}  scc13 = {bool(CS.scc13)}  scc14 = {bool(CS.scc14)}  mdps12 = {bool(CS.mdps12)}')

    new_actuators = actuators.copy()
    new_actuators.steer = apply_steer / self.params.STEER_MAX
    new_actuators.steerOutputCan = apply_steer
    new_actuators.steeringAngleDeg = apply_angle
    new_actuators.accel = accel

    self.frame += 1
    return new_actuators, can_sends

  def create_button_messages(self, CC: car.CarControl, CS: car.CarState, use_clu11: bool):
    can_sends = []
    if use_clu11:
      if CC.cruiseControl.cancel:
        can_sends.append(hyundaican.create_clu11(self.packer, Buttons.CANCEL, self.CP.sccBus, CS.clu11))
      elif CC.cruiseControl.resume:
        # send resume at a max freq of 10Hz
        if (self.frame - self.last_button_frame) * DT_CTRL > 0.1:
          # send 25 messages at a time to increases the likelihood of resume being accepted
          can_sends.extend([hyundaican.create_clu11(self.packer, Buttons.RES_ACCEL, self.CP.sccBus, CS.clu11)] * 25)
          if (self.frame - self.last_button_frame) * DT_CTRL >= 0.15:
            self.last_button_frame = self.frame
    else:
      if (self.frame - self.last_button_frame) * DT_CTRL > 0.25:
        # cruise cancel
        if CC.cruiseControl.cancel:
          if self.CP.flags & HyundaiFlags.CANFD_ALT_BUTTONS:
            can_sends.append(hyundaicanfd.create_acc_cancel(self.packer, self.CP, self.CAN, CS.cruise_info))
            self.last_button_frame = self.frame
          else:
            for _ in range(20):
              can_sends.append(hyundaicanfd.create_buttons(self.packer, self.CP, self.CAN, CS.buttons_counter+1, Buttons.CANCEL))
            self.last_button_frame = self.frame

        # cruise standstill resume
        elif CC.cruiseControl.resume:
          if self.CP.flags & HyundaiFlags.CANFD_ALT_BUTTONS:
            # TODO: resume for alt button cars
            pass
          else:
            for _ in range(20):
              can_sends.append(hyundaicanfd.create_buttons(self.packer, self.CP, self.CAN, CS.buttons_counter+1, Buttons.RES_ACCEL))
            self.last_button_frame = self.frame

    return can_sends
