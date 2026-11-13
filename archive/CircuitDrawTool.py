# Coze运行时相关
from runtime import Args
from typing import Dict, Any

# 内存中的文件处理
from io import BytesIO
# 网络请求
import requests
# 异常调试（云端排错）
import traceback

# 电路绘图核心库
import schemdraw
import schemdraw.elements as elm
# 绘图渲染/后端配置
import matplotlib.pyplot as plt

# 设置中文字体，解决中文标签显示问题
plt.rcParams['font.sans-serif'] = ['Arial', 'SimHei', 'Microsoft YaHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False  # 解决负号显示异常

class LEDDriver(elm.ElementCompound):
    """
    LED 驱动模块
    锚点：
        signal : 控制信号输入端（接 MCU 引脚）
        gnd    : 接地端
    """

    def __init__(self, r_value: str = '220Ω', **kwargs):
        """
        初始化 LED 驱动模块

        Args:
            r_value: 限流电阻阻值，默认为 '220Ω'
            **kwargs: 传递给父类 ElementCompound 的其他参数
        """
        self.r_value = r_value
        super().__init__(**kwargs)

    def setup(self):
        # 信号输入端锚点
        signal_dot = self.add(elm.Dot())

        # 限流电阻（向右），使用传入的阻值标签
        self.add(elm.Resistor().label(self.r_value, loc='top').right())

        # LED（向右，阳极接电阻，阴极接地）
        led = self.add(elm.LED().right())

        # 接地端（从 LED 阴极向下引线并接地）
        self.add(elm.Line().at(led.end).down().length(1))
        gnd_dot = self.add(elm.Ground())

        # 注册锚点
        self.anchors['signal'] = signal_dot.center
        self.anchors['gnd'] = gnd_dot.center

class IndependentButton(elm.ElementCompound):
    """
    独立按键电路模块 (带 Vdd 和上拉电阻)

    锚点:
        vdd    : 电源正极 (顶部)
        signal : 信号输出端 (右侧，用于连接 MCU GPIO)
        gnd    : 电源负极 (底部)
    """

    def __init__(self, r_value: str = '10kΩ', **kwargs):
        """
        初始化按键模块

        Args:
            r_value: 上拉电阻阻值，默认为 '10kΩ'
            **kwargs: 传递给父类 ElementCompound 的其他参数
        """
        self.r_value = r_value
        super().__init__(**kwargs)

    def setup(self):
        # 1. 电源 Vdd (自带箭头和标签)
        vdd = self.add(elm.Vdd().label('VDD', loc='top'))

        # 2. 上拉电阻 (从 Vdd 向下)
        self.add(elm.Resistor().down().label(self.r_value, loc='top'))

        # 3. 电阻下端的连接点 (mid)
        mid = self.add(elm.Dot())

        # 4. 从 mid 向右引出信号线
        self.add(elm.Line().left().length(2))
        signal_dot = self.add(elm.Dot())

        # 5. 直接在mid下方放置按键，删除多余连线 ✅
        self.add(elm.Button().at(mid.center).down().label('K1', loc='top'))

        # 6. 按键直接接地，删除多余连线 ✅
        gnd = self.add(elm.Ground())

        # 7. 注册锚点
        self.anchors['vdd'] = vdd.center
        self.anchors['signal'] = signal_dot.center
        self.anchors['gnd'] = gnd.center

class MotorDriver(elm.ElementCompound):
    """
    电机驱动电路模块 (PNP三极管 + 续流二极管)
    锚点:
        base   : 基极信号输入 (接MCU GPIO)
        vdd    : 电源正极 (+9V)
        gnd    : 接地
    """

    def __init__(self, r_base: str = '1K', diode_label: str = '1N5818', **kwargs):
        self.r_base = r_base
        self.diode_label = diode_label
        super().__init__(**kwargs)

    def setup(self):
        # 1. 基极限流电阻
        R = self.add(elm.Resistor().label(self.r_base).idot(open=True))
        # 2. PNP三极管，方向向上，基极连接
        Q = self.add(elm.BjtPnp().anchor('base'))

        # 3. 【修改点】发射极先向上画一条线，再接+Vcc电源（添加了连线）
        self.add(elm.Line().up().at(Q.emitter).length(1))
        self.add(elm.Vdd(lead=False).label('+Vcc'))

        # 4. 集电极接电机
        self.add(elm.Motor().down().at(Q.collector).idot().dot().label('Motor'))
        # 5. 电机接地
        gnd = self.add(elm.Ground())
        # 6. 续流二极管回路
        self.add(elm.Line().right(1))
        self.add(elm.Schottky().up().label(self.diode_label, 'bottom'))
        self.add(elm.Line().tox(Q.emitter))

        # 注册锚点
        self.anchors['base'] = R.start
        self.anchors['vdd'] = Q.emitter
        self.anchors['gnd'] = gnd.center

class SpeakerDriver(elm.ElementCompound):
    """
    扬声器驱动电路模块 (PNP三极管，无续流二极管)

    锚点:
        base   : 基极信号输入 (接MCU GPIO)
        vdd    : 电源正极 (+Vcc)
        gnd    : 接地
    """

    def __init__(self, r_base: str = '1K', **kwargs):
        """
        初始化扬声器驱动模块

        Args:
            r_base: 基极限流电阻阻值，默认为 '1K'
            **kwargs: 传递给父类 ElementCompound 的其他参数
        """
        self.r_base = r_base
        super().__init__(**kwargs)

    def setup(self):
        # 1. 基极限流电阻（右端带开路节点）
        R = self.add(elm.Resistor().label(self.r_base).idot(open=True))

        # 2. PNP三极管，基极连接电阻右端
        Q = self.add(elm.BjtPnp().anchor('base'))

        # 3. 【统一修改】发射极添加短连接线，再接电源
        self.add(elm.Line().up().at(Q.emitter).length(1))
        self.add(elm.Vdd(lead=False).label('+Vcc'))

        # 4. 集电极接扬声器（向下）
        self.add(elm.Speaker().right().scale(0.4).at(Q.collector).label('SPK', loc='right'))

        # 5. 扬声器下端接地
        gnd = self.add(elm.Ground())

        # 注册锚点
        self.anchors['base'] = R.start  # 信号输入端（电阻左端）
        self.anchors['vdd'] = Q.emitter  # 电源正极
        self.anchors['gnd'] = gnd.center  # 接地

class RCLowPassFilter(elm.ElementCompound):
    """
    RC 低通滤波器模块

    锚点:
        in   : 信号输入端 (V_in)
        out  : 信号输出端 (V_o)
        gnd  : 接地
    """

    def __init__(self, r_value: str = 'R', c_value: str = 'C', **kwargs):
        self.r_value = r_value
        self.c_value = c_value
        super().__init__(**kwargs)

    def setup(self):
        # 1. 电阻，左端实心点，右端空心点
        R = self.add(elm.Resistor().dot().idot(open=True).label(self.r_value))
        self.add(elm.Label('$V_{in}$', loc='left').at(R.start))

        # 2. 从电阻右端向右画输出导线，并在末端加空心点
        out_line = self.add(elm.Line().right(1).label('$V_o$', loc='top').dot(open=True))

        # 3. 回到电阻右端向下接电容
        C = self.add(elm.Capacitor().at(R.end).down().label(self.c_value, loc='top'))
        gnd = self.add(elm.Ground(lead=False))

        # 4. 注册锚点
        self.anchors['in'] = R.start
        self.anchors['out'] = out_line.end  # 输出导线终点
        self.anchors['gnd'] = gnd.center

class MCUBlackBox(elm.Ic):
    """
    简化版MCU黑箱模块。
    仅右侧暴露16个GPIO引脚 (IO0-IO15)。
    锚点: io0, io1, ..., io15
    """

    def __init__(self, *args, **kwargs):
        # 用于存储16个引脚配置的列表
        pins = []

        # 循环生成16个右侧引脚的配置
        for i in range(16):
            # 创建单个引脚对象
            pin = elm.IcPin(
                name=f'IO{i}',  # 引脚逻辑名称，同时也是外部访问的锚点名（如 mcu.io0）
                pin=str(i + 1),  # 引脚物理编号，在符号上显示的数字（1~16）
                side='R',  # 引脚位于芯片右侧（Right）
                # 修正slot：i=0时slot=1/16（最顶部），i=15时slot=16/16=1（最底部）
                # slot=f'{i + 1}/16'
                slot=f'{16 - i}/16'  # 引脚在右侧边缘的纵向位置，从下到上均匀分布
                # 例如 i=0 时 slot='16/16'（最顶部），i=15 时 slot='1/16'（最底部）
            )
            pins.append(pin)

        # 调用父类 Ic 的初始化方法，传入引脚列表和外观参数
        super().__init__(
            pins=pins,  # 引脚配置列表
            size=(2, 5),  # 芯片主体的宽度和高度（单位：unit）
            pinspacing=0.3,  # 相邻引脚之间的间距
            plblofst=.05,  # 引脚标签与芯片边缘的偏移量
            botlabel='MCU',  # 芯片底部显示的标签文字
            **kwargs  # 透传其他可选参数（如颜色、旋转等）
        )

class STC89C52(elm.ElementCompound):
    """STC89C52 单片机 DIP40 封装，可复用元件"""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def setup(self):
        # 绘制 DIP40 芯片并添加引脚标签
        self.chip = (elm.IcDIP(pins=40, size=(6, 10), botlabel='STC89C52')
             .label('T2/P1.0', loc='p1')
             .label('T2EX/P1.1', loc='p2')
             .label('P1.2', loc='p3')
             .label('P1.3', loc='p4')
             .label('P1.4', loc='p5')
             .label('MOSI/P1.5', loc='p6')
             .label('MISO/P1.6', loc='p7')
             .label('SCK/P1.7', loc='p8')
             .label('RST', loc='p9')
             .label('RXD/P3.0', loc='p10')
             .label('TXD/P3.1', loc='p11')
             .label(r'$\overline{\text{INT0}}$/P3.2', loc='p12')
             .label(r'$\overline{\text{INT1}}$/P3.3', loc='p13')
             .label('T0/P3.4', loc='p14')
             .label('T1/P3.5', loc='p15')
             .label(r'$\overline{\text{WR}}$/P3.6', loc='p16')
             .label(r'$\overline{\text{RD}}$/P3.7', loc='p17')
             .label('XTAL2', loc='p18')
             .label('XTAL1', loc='p19')
             .label('VSS', loc='p20')
             .label('P2.0', loc='p21')
             .label('P2.1', loc='p22')
             .label('P2.2', loc='p23')
             .label('P2.3', loc='p24')
             .label('P2.4', loc='p25')
             .label('P2.5', loc='p26')
             .label('P2.6', loc='p27')
             .label('P2.7', loc='p28')
             .label(r'$\overline{\text{PSEN}}$', loc='p29')
             .label(r'ALE/$\overline{\text{PROG}}$', loc='p30')
             .label(r'$\overline{\text{EA}}/V_{PP}$', loc='p31')
             .label('P0.7', loc='p32')
             .label('P0.6', loc='p33')
             .label('P0.5', loc='p34')
             .label('P0.4', loc='p35')
             .label('P0.3', loc='p36')
             .label('P0.2', loc='p37')
             .label('P0.1', loc='p38')
             .label('P0.0', loc='p39')
             .label('VCC', loc='p40')
        )
        self.add(self.chip)

        # 注册引脚锚点：外部端点直接用 p1, p2, ... p40（这些都是 Point 坐标）
        for i in range(1, 41):
            self.anchors[f'p{i}'] = getattr(self.chip, f'p{i}')

class STC89C52_MinSystem(elm.ElementCompound):
    """STC89C52 最小系统（晶振 + 复位 + 电源），布局干净无重叠"""
    def setup(self):
        chip = self.add(STC89C52())

        # 引脚锚点
        xtal2 = chip.anchors['p18']   # 上侧
        xtal1 = chip.anchors['p19']   # 下侧
        rst    = chip.anchors['p9']
        vcc    = chip.anchors['p40']
        vss    = chip.anchors['p20']

        # ========== 晶振电路（完美最终版） ==========
        # 1. 引脚 → 主线
        line_xtal2 = self.add(elm.Line().left(1.5).at(xtal2).dot())
        line_xtal1 = self.add(elm.Line().left(1.5).at(xtal1).dot())

        # 2. 主线 → 晶振
        self.add(elm.Crystal().at(line_xtal2.end).to(line_xtal1.end).label('12MHz', loc='top'))

        # 3. 晶振 → 延长线
        extend_top = self.add(elm.Line().left(3).at(line_xtal2.end))
        extend_bot = self.add(elm.Line().left(3).at(line_xtal1.end))

        # 4. 延长线 → 电容
        cap_top = self.add(elm.Capacitor().left(1.0).at(extend_top.end).label('30pF', loc='bottom'))
        cap_bot = self.add(elm.Capacitor().left(1.0).at(extend_bot.end).label('30pF', loc='bottom'))

        # 🔥 新增：电容之后 → 水平延长线（你要的这一步！）
        cap_extend_top = self.add(elm.Line().left(1.0).at(cap_top.end).dot())
        cap_extend_bot = self.add(elm.Line().left(1.0).at(cap_bot.end).dot())

        # 5. 连接延长线末端
        self.add(elm.Line().at(cap_extend_bot.end).to(cap_extend_top.end))

        # 6. 统一单接地
        gnd_line = self.add(elm.Line().down(1.0).at(cap_extend_top.end))
        self.add(elm.Ground().at(gnd_line.end))

        # ========== 复位电路 ==========
        # 从 RST 向左引出一条短线，末端打实心节点
        line_rst = self.add(elm.Line().left(7).at(rst).dot())
        mid = line_rst.end   # 节点坐标

        # 上拉电阻向上接 Vdd
        res_rst = self.add(elm.Resistor().up().at(mid).label('10k', loc='bottom'))
        self.add(elm.Vdd().at(res_rst.end).label('+5V'))

        # 按键向下，并接地
        btn = self.add(elm.Button().down().at(mid).label('RST', loc='bottom'))
        self.add(elm.Line().down(0.6).at(btn.end))
        self.add(elm.Ground())

        # ========== 电源 ==========
        self.add(elm.Line().right(2).at(vcc))
        self.add(elm.Vdd().label('+5V'))
        self.add(elm.Capacitor(lead=False).down().label('100nF', loc='bottom'))
        self.add(elm.Ground())

        self.add(elm.Line().left(1.3).at(vss))
        self.add(elm.Line().down(1))
        self.add(elm.Ground())

        # ========== 对外 I/O 锚点 ==========
        # 锚点命名规则：p<端口号>_<位号>
        # 例如 p1_0 对应 P1.0 引脚，p3_1 对应 P3.1 引脚（即 TXD）
        # 物理位置：P1 和 P3 在芯片左侧，P2 和 P0 在芯片右侧

        # P1 口（左侧，p1~p8）
        for i in range(8):
            self.anchors[f'p1_{i}'] = chip.anchors[f'p{i+1}']

        # P2 口（右侧，p21~p28）
        for i in range(8):
            self.anchors[f'p2_{i}'] = chip.anchors[f'p{21+i}']

        # P3 口（左侧，p10~p17）—— 注意 P3.0=RXD, P3.1=TXD
        for i in range(8):
            self.anchors[f'p3_{i}'] = chip.anchors[f'p{10+i}']

        # P0 口（右侧，p39~p32 从上到下递减）
        self.anchors['p0_0'] = chip.anchors['p39']
        self.anchors['p0_1'] = chip.anchors['p38']
        self.anchors['p0_2'] = chip.anchors['p37']
        self.anchors['p0_3'] = chip.anchors['p36']
        self.anchors['p0_4'] = chip.anchors['p35']
        self.anchors['p0_5'] = chip.anchors['p34']
        self.anchors['p0_6'] = chip.anchors['p33']
        self.anchors['p0_7'] = chip.anchors['p32']

        # ------------------------------------------------------------
        # 电源与地
        # ------------------------------------------------------------
        self.anchors['vcc'] = chip.anchors['p40']   # 芯片右上角
        self.anchors['gnd'] = chip.anchors['p20']   # 芯片左下角

        # ========== 芯片几何中心，求取安全布线锚点 ==========
        # 取左上角 p1 和右下角 p40 的中点作为芯片中心
        p1 = chip.anchors['p1']
        p40 = chip.anchors['p40']
        center_x = (p1[0] + p40[0]) / 2
        center_y = (p1[1] + p40[1]) / 2
        self.anchors['center'] = (center_x, center_y)

        # ========== 安全布线锚点 ==========
        # 左侧安全区：芯片左边界再向左 10 单位，Y 固定在 P1.0 上方 7 单位（避开所有左侧电路）
        left_x = p1[0] - 10
        left_y = p1[1] + 7
        self.anchors['conn_left'] = (left_x, left_y)

        # 右侧安全区：芯片右边界再向右 2 单位，Y 不变（右侧空旷，仅水平延伸即可）
        right_x = chip.anchors['p21'][0] + 2
        # 右侧安全区 Y 坐标任取，布线时我们只用到 X，但为完整性设为与中心同高
        self.anchors['conn_right'] = (right_x, center_y)

class STM32F103C8(elm.ElementCompound):
    """
    STM32F103C8T6 LQFP48 封装芯片
    纯引脚定义，无电源/复位/晶振，使用 elm.Ic 自定义绘制（仿官方JK触发器）
    左侧引脚：1~24 从上到下 | 右侧引脚：48~25 从上到下
    """
    def setup(self):
        self.ic = self.add(elm.Ic(
            pins=[
                # ==================== 左侧引脚 (LEFT) 1~24 从上到下 ====================
                elm.IcPin(name='VBAT',      pin='1',  side='left'),
                elm.IcPin(name='PC13',      pin='2',  side='left'),
                elm.IcPin(name='PC14',      pin='3',  side='left'),
                elm.IcPin(name='PC15',      pin='4',  side='left'),
                elm.IcPin(name='PD0/OSC_IN',pin='5',  side='left'),
                elm.IcPin(name='PD1/OSC_OUT',pin='6', side='left'),
                elm.IcPin(name='NRST',      pin='7',  side='left'),
                elm.IcPin(name='VSSA',      pin='8',  side='left'),
                elm.IcPin(name='VDDA',      pin='9',  side='left'),
                elm.IcPin(name='PA0',       pin='10', side='left'),
                elm.IcPin(name='PA1',       pin='11', side='left'),
                elm.IcPin(name='PA2',       pin='12', side='left'),
                elm.IcPin(name='PA3',       pin='13', side='left'),
                elm.IcPin(name='PA4',       pin='14', side='left'),
                elm.IcPin(name='PA5',       pin='15', side='left'),
                elm.IcPin(name='PA6',       pin='16', side='left'),
                elm.IcPin(name='PA7',       pin='17', side='left'),
                elm.IcPin(name='PB0',       pin='18', side='left'),
                elm.IcPin(name='PB1',       pin='19', side='left'),
                elm.IcPin(name='PB2/BOOT1', pin='20', side='left'),
                elm.IcPin(name='PB10',      pin='21', side='left'),
                elm.IcPin(name='PB11',      pin='22', side='left'),
                elm.IcPin(name='VSS',       pin='23', side='left'),
                elm.IcPin(name='VDD',       pin='24', side='left'),

                # ==================== 右侧引脚 (RIGHT) 48~25 从上到下 ====================
                elm.IcPin(name='VDD3',      pin='48', side='right'),
                elm.IcPin(name='VSS3',      pin='47', side='right'),
                elm.IcPin(name='BOOT0',     pin='46', side='right'),
                elm.IcPin(name='PB9',       pin='45', side='right'),
                elm.IcPin(name='PB8',       pin='44', side='right'),
                elm.IcPin(name='PB7',       pin='43', side='right'),
                elm.IcPin(name='PB6',       pin='42', side='right'),
                elm.IcPin(name='PB5',       pin='41', side='right'),
                elm.IcPin(name='PB4',       pin='40', side='right'),
                elm.IcPin(name='PB3',       pin='39', side='right'),
                elm.IcPin(name='PA15',      pin='38', side='right'),
                elm.IcPin(name='PA14/SWCLK',pin='37', side='right'),
                elm.IcPin(name='VDD2',      pin='36', side='right'),
                elm.IcPin(name='VSS2',      pin='35', side='right'),
                elm.IcPin(name='PA13/SWDIO',pin='34', side='right'),
                elm.IcPin(name='PA12',      pin='33', side='right'),
                elm.IcPin(name='PA11',      pin='32', side='right'),
                elm.IcPin(name='PA10',      pin='31', side='right'),
                elm.IcPin(name='PA9',       pin='30', side='right'),
                elm.IcPin(name='PA8',       pin='29', side='right'),
                elm.IcPin(name='PB15',      pin='28', side='right'),
                elm.IcPin(name='PB14',      pin='27', side='right'),
                elm.IcPin(name='PB13',      pin='26', side='right'),
                elm.IcPin(name='PB12',      pin='25', side='right'),
            ],
            edgepadW=0.8,
            pinspacing=0.6,
            linewidth=1
        ).label('STM32F103C8T6', 'bottom', fontsize=12))

        # 引脚编号锚点 p1~p48
        for pin_num in range(1, 49):
            self.anchors[f'p{pin_num}'] = self.ic.anchors[f'pin{pin_num}']

        # 功能锚点
        self.anchors['NRST']   = self.ic.anchors['pin7']
        self.anchors['BOOT0']  = self.ic.anchors['pin46']
        self.anchors['BOOT1']  = self.ic.anchors['pin20']
        self.anchors['OSC_IN'] = self.ic.anchors['pin5']
        self.anchors['OSC_OUT']= self.ic.anchors['pin6']
        self.anchors['SWDIO']  = self.ic.anchors['pin34']
        self.anchors['SWCLK']  = self.ic.anchors['pin37']

        # ==================== 修复：新增电源锚点（解决KeyError） ====================
        self.anchors['VDD'] = self.ic.anchors['pin24']
        self.anchors['VSS'] = self.ic.anchors['pin23']
        self.anchors['VDD2'] = self.ic.anchors['pin36']
        self.anchors['VSS2'] = self.ic.anchors['pin35']
        self.anchors['VDD3'] = self.ic.anchors['pin48']
        self.anchors['VSS3'] = self.ic.anchors['pin47']

        # GPIO 名称锚点
        self.anchors['PA0'] = self.ic.anchors['pin10']
        self.anchors['PA1'] = self.ic.anchors['pin11']
        self.anchors['PA2'] = self.ic.anchors['pin12']
        self.anchors['PA3'] = self.ic.anchors['pin13']
        self.anchors['PA4'] = self.ic.anchors['pin14']
        self.anchors['PA5'] = self.ic.anchors['pin15']
        self.anchors['PA6'] = self.ic.anchors['pin16']
        self.anchors['PA7'] = self.ic.anchors['pin17']
        self.anchors['PA8'] = self.ic.anchors['pin29']
        self.anchors['PA9'] = self.ic.anchors['pin30']
        self.anchors['PA10'] = self.ic.anchors['pin31']
        self.anchors['PA11'] = self.ic.anchors['pin32']
        self.anchors['PA12'] = self.ic.anchors['pin33']
        self.anchors['PA13'] = self.ic.anchors['pin34']
        self.anchors['PA14'] = self.ic.anchors['pin37']
        self.anchors['PA15'] = self.ic.anchors['pin38']

        self.anchors['PB0'] = self.ic.anchors['pin18']
        self.anchors['PB1'] = self.ic.anchors['pin19']
        self.anchors['PB2'] = self.ic.anchors['pin20']
        self.anchors['PB3'] = self.ic.anchors['pin39']
        self.anchors['PB4'] = self.ic.anchors['pin40']
        self.anchors['PB5'] = self.ic.anchors['pin41']
        self.anchors['PB6'] = self.ic.anchors['pin42']
        self.anchors['PB7'] = self.ic.anchors['pin43']
        self.anchors['PB8'] = self.ic.anchors['pin44']
        self.anchors['PB9'] = self.ic.anchors['pin45']
        self.anchors['PB10'] = self.ic.anchors['pin21']
        self.anchors['PB11'] = self.ic.anchors['pin22']
        self.anchors['PB12'] = self.ic.anchors['pin25']
        self.anchors['PB13'] = self.ic.anchors['pin26']
        self.anchors['PB14'] = self.ic.anchors['pin27']
        self.anchors['PB15'] = self.ic.anchors['pin28']

        self.anchors['PC13'] = self.ic.anchors['pin2']
        self.anchors['PC14'] = self.ic.anchors['pin3']
        self.anchors['PC15'] = self.ic.anchors['pin4']

        # ==================== 👇 只追加这3行，放在最后即可 ====================
        self.anchors['VDDA'] = self.ic.anchors['pin9']   # 模拟电源
        self.anchors['VSSA'] = self.ic.anchors['pin8']   # 模拟地
        self.anchors['VBAT'] = self.ic.anchors['pin1']   # 备用电源

class STM32F103C8_MinSystem(elm.ElementCompound):
    """STM32F103C8T6 最小系统（晶振+复位+BOOT+电源），复刻51写法"""
    def setup(self):
        # 1. 添加芯片
        chip = self.add(STM32F103C8())

        # 2. 引脚锚点
        osc_in  = chip.anchors['OSC_IN']
        osc_out = chip.anchors['OSC_OUT']
        nrst    = chip.anchors['NRST']
        boot0   = chip.anchors['BOOT0']
        vdd     = chip.anchors['VDD']
        vss     = chip.anchors['VSS']

        # ==================== 晶振电路（和51完全一样） ====================
        line_osc_out = self.add(elm.Line().left(1.5).at(osc_out).dot())
        line_osc_in  = self.add(elm.Line().left(1.5).at(osc_in).dot())
        self.add(elm.Crystal().at(line_osc_out.end).to(line_osc_in.end).label('8MHz', loc='top'))

        extend_top = self.add(elm.Line().left(1.2).at(line_osc_out.end))
        extend_bot = self.add(elm.Line().left(1.2).at(line_osc_in.end))

        cap_top = self.add(elm.Capacitor().left(1.0).at(extend_top.end).label('30pF', loc='top', fontsize=8))
        cap_bot = self.add(elm.Capacitor().left(1.0).at(extend_bot.end).label('30pF', loc='bottom', fontsize=8))

        cap_extend_top = self.add(elm.Line().left(1.0).at(cap_top.end).dot())
        cap_extend_bot = self.add(elm.Line().left(1.0).at(cap_bot.end).dot())

        self.add(elm.Line().at(cap_extend_bot.end).to(cap_extend_top.end))
        gnd_line = self.add(elm.Line().down(1.0).at(cap_extend_top.end))
        self.add(elm.Ground().at(gnd_line.end))

        # ==================== 复位电路（和51完全一样） ====================
        line_rst = self.add(elm.Line().left(5.5).at(nrst).dot())
        mid = line_rst.end
        res_rst = self.add(elm.Resistor().up().at(mid).label('10k', loc='bottom'))
        self.add(elm.Vdd().at(res_rst.end).label('+3.3V'))
        btn = self.add(elm.Button().down().at(mid).label('RST', loc='top'))
        self.add(elm.Line().down(0.6).at(btn.end))
        self.add(elm.Ground())

        # ==================== 电源电路（和51完全一样） ====================
        self.add(elm.Line().left(2).at(vdd))
        self.add(elm.Vdd().label('+3.3V'))
        self.add(elm.Capacitor(lead=False).down().label('100nF', loc='top'))
        self.add(elm.Ground())

        # ==================== ✅ 最终版：按要求合并接地（无交叉、布局清爽） ====================
        # ① 左侧：VSS 和 BOOT1 合并接地（两条左线 → 连接 → 统一GND）
        vss = chip.anchors['VSS']
        boot1 = chip.anchors['BOOT1']
        line_vss = self.add(elm.Line().left(1.2).at(vss))    # VSS 向左出线
        line_boot1 = self.add(elm.Line().left(1.2).at(boot1)) # BOOT1 向左出线
        self.add(elm.Line().at(line_vss.end).to(line_boot1.end)) # 两条线连在一起
        self.add(elm.Ground().down().at(line_vss.end)) # 统一接地

        # ② 左侧其他电源（不动你已调好的）
        vdda = chip.anchors['VDDA']
        self.add(elm.Line().left(1).at(vdda))
        self.add(elm.Vdd().up().label('+3.3V', fontsize=9))

        vssa = chip.anchors['VSSA']
        self.add(elm.Line().left(1).at(vssa))
        self.add(elm.Ground().down())

        vbat = chip.anchors['VBAT']
        self.add(elm.Line().left(1).at(vbat))
        self.add(elm.Vdd().up().label('+3.3V', fontsize=9))

        # ③ 右侧：VSS3 和 BOOT0 合并接地（两条右线 → 连接 → 统一GND）
        vss3 = chip.anchors['VSS3']
        boot0 = chip.anchors['BOOT0']
        line_vss3 = self.add(elm.Line().right(1.2).at(vss3))    # VSS3 向右出线
        line_boot0 = self.add(elm.Line().right(1.2).at(boot0))  # BOOT0 向右出线
        self.add(elm.Line().at(line_vss3.end).to(line_boot0.end)) # 两条线连在一起
        self.add(elm.Ground().up().at(line_vss3.end)) # 统一接地

        # ④ 右侧其他电源（不动你已调好的）
        vss2 = chip.anchors['VSS2']
        self.add(elm.Line().right(1).at(vss2))
        self.add(elm.Ground().up())

        vdd2 = chip.anchors['VDD2']
        self.add(elm.Line().right(1).at(vdd2))
        self.add(elm.Vdd().down().label('+3.3V', fontsize=9, loc='bottom'))

        vdd3 = chip.anchors['VDD3']
        self.add(elm.Line().right(1).at(vdd3))
        self.add(elm.Vdd().down().label('+3.3V', fontsize=9, loc='bottom'))

        # ==================== 对外IO锚点（仿照51格式） ====================
        # PA口
        for i in range(16):
            self.anchors[f'pa_{i}'] = chip.anchors[f'PA{i}']
        # PB口
        for i in range(16):
            self.anchors[f'pb_{i}'] = chip.anchors[f'PB{i}']
        # PC口
        self.anchors['pc_13'] = chip.anchors['PC13']
        self.anchors['pc_14'] = chip.anchors['PC14']
        self.anchors['pc_15'] = chip.anchors['PC15']

        # 电源/下载口锚点
        self.anchors['vcc'] = vdd
        self.anchors['gnd'] = vss
        self.anchors['swdio'] = chip.anchors['SWDIO']
        self.anchors['swclk'] = chip.anchors['SWCLK']

        # ========== 芯片几何中心与安全布线锚点 ==========
        # 取最左侧PA0和最右侧PA15的中点作为芯片中心
        pa0 = chip.anchors['PA0']
        pa15 = chip.anchors['PA15']
        center_x = (pa0[0] + pa15[0]) / 2
        center_y = (pa0[1] + pa15[1]) / 2
        self.anchors['center'] = (center_x, center_y)

        # 左侧安全区：芯片左边界再向左10单位，Y固定在PA0上方7单位
        left_x = pa0[0] - 14
        left_y = pa0[1] + 12
        self.anchors['conn_left'] = (left_x, left_y)

        # 右侧安全区：芯片右边界再向右2单位，Y取中心
        right_x = pa15[0] + 2
        self.anchors['conn_right'] = (right_x, center_y)

# ========== 图片上传函数（从 ImageRetrievalTool 复制过来） ==========
def upload_to_imgurl(buffer: BytesIO, logger) -> str:
    try:
        imgurl_api_url = "https://www.imgurl.org/api/v2/upload"
        files = {
            'file': ('circuit.png', buffer, 'image/png'),
            'uid': (None, 'IMGURL_UID'),
            'token': (None, 'IMGURL_TOKEN')
        }
        response = requests.post(imgurl_api_url, files=files, timeout=30)
        result = response.json()
        if response.status_code == 200 and result.get('code') == 200:
            return result['data']['url']
        else:
            raise Exception(result.get('msg', '上传失败'))
    except Exception as e:
        logger.error(f"上传异常: {traceback.format_exc()}")
        raise e

# ========== 入口函数 ==========
def handler(args: Args) -> Dict[str, Any]:
    logger = args.logger
    try:
        # ----- 1. 解析输入参数 -----
        raw = args.input
        if isinstance(raw, dict):
            params = raw
        elif hasattr(raw, '__dict__'):
            params = vars(raw)
        else:
            params = {}

        circuit_type = params.get("circuit_type", "").lower().strip()
        mcu_type = params.get("mcu_type", "51").lower().strip()          # 新增：MCU类型
        mcu_pin = params.get("mcu_pin", "").lower().strip()
        if not mcu_pin:               # 用户未提供引脚值时，根据 MCU 类型设置默认值
            if mcu_type == "51":
                mcu_pin = "p2_7"
            elif mcu_type == "stm32":
                mcu_pin = "pb_12"
            # else 情况已在前面的 mcu_type 验证中处理，不会走到这里
        logger.info(f"动态绘图请求: circuit={circuit_type}, mcu={mcu_type}, pin={mcu_pin}, params={params}")

        # ----- 2. 获取可选参数 -----
        use_rc = params.get("use_rc", "false").lower() == "true"
        rc_r = params.get("rc_r", "10kΩ")
        rc_c = params.get("rc_c", "0.1µF")

        # ----- 3. 创建画布 -----
        plt.switch_backend('Agg')
        d = schemdraw.Drawing(unit=2.5)

        # ----- 4. 根据 MCU 类型创建芯片 -----
        if mcu_type == "51":
            mcu = d.add(STC89C52_MinSystem())
        elif mcu_type == "stm32":
            mcu = d.add(STM32F103C8_MinSystem())
        else:
            return {"message": f"不支持的 MCU 类型: {mcu_type}。可选: 51, stm32"}

        # ----- 5. 安全取出引脚绝对坐标 -----
        if mcu_pin not in mcu.absanchors:
            # 根据类型提供可用引脚提示
            if mcu_type == "51":
                available = "p1_0~p1_7, p2_0~p2_7, p3_0~p3_7, p0_0~p0_7"
            else:
                available = "pa_0~pa_15, pb_0~pb_15, pc_13~pc_15"
            return {"message": f"无效引脚: {mcu_pin}。{mcu_type} 可用引脚: {available}"}
        pin_pos = mcu.absanchors[mcu_pin]

        # ----- 6. 计算芯片中心，判断引脚左右 -----
        if 'center' in mcu.absanchors:
            cx, cy = mcu.absanchors['center']
        else:
            # 备选方案：用已知引脚估算
            if mcu_type == "51":
                cx = (mcu.absanchors['p1_0'][0] + mcu.absanchors['p2_7'][0]) / 2
                cy = (mcu.absanchors['p1_0'][1] + mcu.absanchors['p2_7'][1]) / 2
            else:
                cx = (mcu.absanchors['pa_0'][0] + mcu.absanchors['pa_15'][0]) / 2
                cy = (mcu.absanchors['pa_0'][1] + mcu.absanchors['pa_15'][1]) / 2

        px, py = pin_pos

        # ----- 7. 布线到安全区域（复刻本地测试逻辑）-----
        if px < cx:
            # 左侧引脚 → conn_left 安全区
            safe_left = mcu.absanchors['conn_left']
            d.add(elm.Line().at(pin_pos).left().tox(safe_left[0]))
            d.add(elm.Line().up().toy(safe_left[1]))
            d.add(elm.Line().right(0).at(d.here))           # 重置方向
            start_anchor = d.here
        else:
            # 右侧引脚 → conn_right 安全区
            safe_right = mcu.absanchors['conn_right']
            line = d.add(elm.Line().at(pin_pos).right().tox(safe_right[0]))
            start_anchor = line.end

        # ----- 8. 可选 RC 滤波器 -----
        if use_rc:
            rc = d.add(RCLowPassFilter(r_value=rc_r, c_value=rc_c).anchor('in').at(start_anchor))
            start_anchor = rc.absanchors['out']   # 必须用绝对锚点

        # ----- 9. 根据电路类型放置目标外设模块 -----
        if circuit_type == "led":
            r = params.get("r", "220Ω")
            d.add(LEDDriver(r_value=r).anchor('signal').at(start_anchor))
        elif circuit_type == "button":
            r = params.get("r", "10kΩ")
            d.add(IndependentButton(r_value=r).anchor('signal').at(start_anchor))
        elif circuit_type == "rc_filter":
            if not use_rc:   # 如果没有在 use_rc 中提前画好，则单独绘制
                d.add(RCLowPassFilter(r_value=rc_r, c_value=rc_c).anchor('in').at(start_anchor))
        elif circuit_type == "buzzer":
            r = params.get("r_base", "1kΩ")
            d.add(SpeakerDriver(r_base=r).anchor('base').at(start_anchor))
        elif circuit_type == "motor":
            r = params.get("r_base", "1kΩ")
            diode = params.get("diode", "1N5818")
            d.add(MotorDriver(r_base=r, diode_label=diode).anchor('base').at(start_anchor))
        else:
            return {"message": f"不支持的动态电路类型: {circuit_type}"}

        # ----- 10. 保存、上传、返回结果 -----
        buffer = BytesIO()
        d.draw()
        plt.savefig(buffer, format='png', dpi=150, bbox_inches='tight')
        buffer.seek(0)
        plt.close()

        img_url = upload_to_imgurl(buffer, logger)
        markdown = f"![{circuit_type}电路图]({img_url})"
        return {
            "message": f"为您生成 {circuit_type} 电路图 (MCU: {mcu_type}, 引脚: {mcu_pin})\n\n{markdown}",
            "image_url": img_url
        }

    except Exception as e:
        logger.error(traceback.format_exc())
        return {"message": f"电路图生成失败：{str(e)}"}
