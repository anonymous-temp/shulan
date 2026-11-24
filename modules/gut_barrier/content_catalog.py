"""Versioned content for the supplied five-chapter report, not clinical rules.

Case-specific diagnoses, sample risk numbers and unchecked doses are deliberately
not templates. Incomplete bibliography entries remain explicit source records.
"""
from copy import deepcopy

CONTENT_VERSION = "gut-barrier-content-20261010-v4-barrier-only"
TEMPLATE_VERSION = "gut-barrier-five-chapters-20261010-v4-barrier-only"
REPORT_TITLE = "肠道屏障功能评估报告"
REPORT_SOURCE = {
    "name": "张学斌肠道屏障功能与慢性炎症综合评估报告(1).docx",
    "sha256": "8c593463f065739de1eeac0f7c6c1d347f8a4f4fee2146a552da40c331a58e69",
}
CHAPTERS = (
    ("overview", "第一章 肠道屏障检测结果综述"),
    ("disease_risks", "第二章 相关疾病风险评估"),
    ("repair_plan", "第三章 分层修复方案"),
    ("education", "第四章 肠道屏障与慢性炎症"),
    ("appendix", "第五章 附录"),
)
DISEASE_SECTION_ORDER = ("digestive", "metabolic", "cardiovascular", "psychiatric", "neurological", "immune", "oncology")
EDUCATION = (
    ("4.1 肠道屏障：一堵“会呼吸”的四重城墙",
     "肠道屏障是营养吸收与免疫防御共用的界面，并非完全封闭的隔离层。成人消化道黏膜表面积的一项研究估计约为32平方米；"
     "如此广阔的界面需要持续更新的上皮细胞、细胞间连接及黏膜微环境协同维持。[1]\n\n"
     "机械屏障：单层肠上皮与紧密连接共同限制细胞间物质通过，闭合蛋白（occludin）、密封蛋白（claudin）及闭锁小带蛋白（ZO-1）"
     "等构成可调节的连接结构。化学屏障：杯状细胞分泌的黏液和抗菌肽减少微生物直接接触上皮。生物屏障：共生菌群占据生态位，"
     "参与营养底物代谢并抑制部分机会致病菌扩张。免疫屏障：肠相关淋巴组织、分泌型IgA及黏膜免疫细胞，在病原防御与食物、共生菌耐受之间保持平衡。[2,3]\n\n"
     "四重屏障彼此依赖。膳食纤维经菌群发酵产生乙酸、丙酸、丁酸等短链脂肪酸，其中丁酸是结肠上皮的重要能量底物，"
     "并参与紧密连接组装和黏膜免疫调节。上皮更新不仅需要能量和合成原料，也需要菌群代谢提供适宜的局部信号；"
     "因此，屏障养护同时关注上皮营养、黏液保护和菌群生境。[补1]"),
    ("4.2 城墙失守：肠道屏障受损的发生与发展",
     "肠道屏障受损既可能发生在细胞间连接，也可能涉及上皮细胞、黏液保护及菌群生态。感染、酒精、部分药物暴露和不利饮食模式等，"
     "可通过不同途径干扰屏障稳态；慢性压力、睡眠与原有疾病则可能影响其恢复。连蛋白（zonulin）是参与连接调节的因子之一，"
     "其介导的信号可改变紧密连接的组织状态，但屏障变化并非由单一“开关”决定。[3]\n\n"
     "从机制上可概括为：黏液保护减弱与菌群生态改变，使上皮接触更多腔内刺激；紧密连接蛋白的表达或定位变化，增加细胞间通透；"
     "若炎症和损伤持续，上皮细胞脱落、修复负担及抗原移位又可能进一步增加。这些环节可以相互影响，并非每个人都按固定顺序发展。"
     "腹胀、腹痛、排便改变等可与相关疾病同时出现，但缺乏特异性；没有明显症状或内镜形态异常，也不等同于屏障调节功能完全正常。\n\n"
     "检测中的D-乳酸、DAO与LPS分别提供微生物代谢产物、上皮相关酶活性及内毒素相关信号的观察线索。联合解读有助于明确进一步评估的环节，"
     "其数值变化与黏膜活检、病原学或直接通透性检查所提供的信息并不相同。"),
    ("4.3 慢火点燃：慢性低度炎症的发生机制",
     "脂多糖（LPS）是革兰阴性菌外膜成分。当屏障防御和机体清除能力不足时，细菌产物与免疫系统接触增加，"
     "可形成持续的炎症刺激。动物实验提出了低水平内毒素暴露与代谢改变之间的机制联系；小样本人体餐后研究也观察到内毒素信号变化，"
     "但这两类研究的条件与个体日常检测并不相同。[4,5]\n\n"
     "其主要信号链为：LPS经结合蛋白及CD14递呈，由TLR4/MD-2受体复合物识别，激活NF-κB等细胞内通路，"
     "促进TNF-α、IL-1β、IL-6等炎症介质产生，参与机体免疫反应及炎症信号传递。[6,7]\n\n"
     "慢性低度炎症强调刺激的持续性与调节失衡，常不表现为剧烈的红、肿、热、痛。长期炎症信号可干扰胰岛素反应、血管内皮功能及组织修复，"
     "使代谢负担与免疫激活相互叠加。这条机制解释了肠道状态为何值得与全身炎症一起观察；个体的持续性及来源仍需病程和多方面资料支持。"),
    ("4.4 互为因果：屏障受损与慢性炎症的放大环路",
     "炎症不仅可能是屏障变化的后果，也会反过来改变屏障。实验研究显示，TNF相关信号能够激活肌球蛋白轻链激酶（MLCK），"
     "改变连接周围的细胞骨架张力，并促使闭合蛋白从细胞膜内吞，增加上皮通透性；连接蛋白的定位改变可以早于明显的组织结构破坏。"
     "这一证据来自细胞及动物模型，是理解炎症反向影响连接结构的依据。[补3]\n\n"
     "与此同时，持续炎症可改变肠腔营养及氧化还原环境，影响共生菌生态和短链脂肪酸供给；上皮能量支持不足、黏液防御减弱又会增加修复负担。"
     "由此形成“屏障保护减弱→抗原接触增加→免疫激活→连接调节异常与菌群改变→屏障进一步受影响”的放大环路。[3,4,7]\n\n"
     "阻断环路需要同时处理诱因、原有疾病、营养不足与微生态环境，而非仅追求某一个数值下降。复评时应把症状、膳食耐受、营养状态及检测趋势放在一起，"
     "观察修复是否稳定，并避免把短期指标波动解释为整个病理过程已经逆转。"),
    ("4.5 慢性疾病的“共同土壤”",
     "肠道与远端器官通过血液、免疫、代谢及神经信号联系，统称为肠–X轴。肠–肝轴中，门静脉把来自肠道的物质输送至肝脏，"
     "细菌产物可参与肝内免疫细胞激活，叠加脂质代谢压力。肠–代谢轴中，炎症相关的IKK/JNK等信号可干扰胰岛素受体下游传递，"
     "这有助于解释肥胖、脂肪性肝病与2型糖尿病之间交织的代谢和炎症背景。[4,6,7]\n\n"
     "肠–心血管轴关注细菌产物、炎症介质和代谢物与血管内皮、动脉粥样硬化之间的联系，实际心血管评估还需结合血脂、血压、吸烟及既往病史。"
     "肠–脑轴涉及免疫信号、迷走神经及微生物代谢物；小鼠实验观察到菌群变化与血脑屏障通透性之间的联系，"
     "但不能由此把个人屏障三项结果换算成阿尔茨海默病或帕金森病的发病率。[补8]\n\n"
     "在免疫系统，反复的抗原接触可能影响耐受与免疫激活平衡，并与遗传易感性、感染和环境暴露共同参与疾病过程。"
     "所谓“共同土壤”指多个疾病共享部分机制，不代表同一原因或必然先后发生。相关方向用于理解第二章的机制关联，"
     "具体病种风险需同时考虑该病种的临床资料；群体患病率也不能直接作为受检者的个人风险指数。[3,7]"),
    ("4.6 炎症性衰老：当慢火遇上时间",
     "“炎症性衰老”（inflammaging）描述增龄过程中持续低度炎症与免疫调节改变相互交织的现象。细胞衰老、代谢压力、慢性抗原刺激和既有疾病"
     "均可参与其中；肠道屏障及菌群代谢是研究关注的环节之一，而不是衰老的唯一决定因素。[8]\n\n"
     "一项探索性人体研究比较了19名18–30岁成人与18名70岁及以上成人，老年组血清zonulin平均较高，并与TNF-α、IL-6及部分体能指标相关。"
     "这是小样本横断面关联，不是随年龄增长的个人连续轨迹，也不能据此推算某一年龄的肠道通透性或炎症倍数。[9]\n\n"
     "随着年龄和健康状态变化，饮食多样性、活动水平、用药及合并疾病也会共同影响菌群生态。保护上皮与菌群赖以维持的营养环境，"
     "保持适宜的有氧和抗阻活动、规律睡眠，并规范管理已有慢性疾病，是长期管理的可行方向。炎症性衰老的评估强调长期趋势与功能状态，"
     "不能将年龄等同于损伤程度，也不能把示意图当作个人检测曲线。"),
)
PRINCIPLES = (
    "去除：通过健康评估识别饮食、生活方式、药物及疾病相关诱因，减少持续的屏障刺激。",
    "改善：通过肠道营养补充提供修复原料、上皮能量和菌群代谢底物。",
    "重建：在明确适应证与安全条件下评估精准肠道菌群移植（PFMT），重建微生态。",
    "修复：以MDF靶向功能菌群调节衔接紧密连接、肠上皮及内毒素相关环节的持续支持。",
    "平衡：通过饮食、运动、睡眠和压力管理维护长期稳态，并按随访结果调整。",
)
REPAIR_INTRO = {
    "nutrition": "肠道营养补充围绕“原料—能量—信号—抗炎”提供支持：蛋白质和相关营养素为上皮更新提供原料；"
                 "谷氨酰胺与菌群产生的短链脂肪酸参与上皮供能；丁酸等代谢物参与连接组装和黏膜信号调节；"
                 "合理脂肪酸摄入与营养缺乏纠正则支持正常免疫调节。经口、肠内和静脉途径分别适用于不同摄入能力及临床条件。",
    "pfmt": "精准肠道菌群移植（PFMT）以经筛查的供者菌群为来源，目标是重建肠道微生态，并与营养及后续菌群维护衔接。"
            "它属于有明确适用条件的医疗干预，不把屏障评分升高或“菌群失调”本身视为已经满足移植适应证。",
    "mdf": "MDF靶向功能菌群调节以功能目标组织菌株与营养底物选择，围绕紧密连接修复、肠上皮修复和内毒素相关环节形成持续支持。"
           "功能目标不等同于具体配方；制剂需要明确菌株编号、活性形式、产品组成及适用人群，具体配方另行进行个体化确认。",
    "lifestyle": "生活方式管理覆盖饮食、运动与行为三方面，目标是减少持续刺激、改善代谢状态，并维持上皮及共生菌群所需的稳定环境。"
                 "以下为一般管理要点；儿童、妊娠期、体弱者及合并疾病人群采用各自适宜的目标。",
}
REPAIR_SECTIONS = (
    ("3.2 第一原则 · 去除：健康评估与诱因去除",
     "修复从完整的健康评估开始。综合消化道症状、既往疾病、近期感染、饮食结构、饮酒、睡眠压力及用药情况，"
     "识别可能影响屏障稳态的因素。对持续腹痛、腹泻、便血或明显体重变化，应先完成相应临床评估，再安排后续修复。"
     "减少已明确不耐受的饮食刺激及不必要的药物暴露；处方药的调整由临床医师决定，不自行停药或长期过度忌口。"),
    ("3.3 第二原则 · 改善：肠道营养补充", REPAIR_INTRO["nutrition"] +
     "\n\n营养组合：通过食物保障能量和优质蛋白，在此基础上评估谷氨酰胺、锌、维生素D、鱼油来源的ω-3脂肪酸以及膳食纤维。"
     "谷氨酰胺与上皮代谢有关；锌和维生素D参与细胞、免疫及屏障稳态，额外补充应区分膳食不足与已证实缺乏。"
     "可发酵纤维为共生菌提供底物；细胞实验中，丁酸通过AMPK相关机制促进紧密连接组装，这支持其作为营养信号的研究价值，"
     "并不等于任意丁酸产品均具有同等临床疗效。[补1]"
     "\n\n实施路径：能安全进食者首先优化口服膳食或口服营养；存在营养不良风险、经口不足且胃肠道可用者，评估肠内营养；"
     "肠内途径不可行或不能满足需要时，再评估肠外支持。丙氨酰谷氨酰胺和鱼油脂肪乳属于医疗营养制剂，"
     "其中鱼油脂肪乳用于静脉营养，不能与普通口服鱼油互换；不依据IBFC等级统一启动输注或固定联合给药。"
     "选择前需核对营养状态、肝肾功能、过敏、用药和监测条件。成人可在耐受范围内逐步增加食物来源纤维，"
     "一般健康目标为每日至少25克；狭窄、梗阻风险或明显不耐受时须另定饮食形式。[补4,6]"),
    ("3.4 第三原则 · 重建：精准肠道菌群移植（PFMT）", REPAIR_INTRO["pfmt"] +
     "\n\n重建阶段：先核对疾病诊断、既往规范治疗和营养状态，评估免疫状态，并完成供者、病原体及制备质量筛查。"
     "现有指南支持在特定复发性艰难梭菌感染等情境中选择微生态移植疗法；对肠易激综合征及多种炎症性肠病，"
     "AGA指南不建议在临床试验之外常规实施。严重免疫抑制者还存在额外安全限制。[补5]"
     "\n\n衔接阶段：重建后的管理可按“菌群重建→MDF功能支持→恢复期维持”衔接。确有移植适应证并实施后，"
     "首先观察症状、耐受及感染相关安全性，再结合膳食和所需功能决定是否衔接MDF；并非移植后必须叠加同一益生菌组合。"
     "维持阶段重在稳定的营养底物、生活方式和复评，是否采用活菌制剂及持续时间取决于具体菌株证据和个体安全条件，"
     "通过症状、耐受及随访变化评价效果并调整后续管理。"),
    ("3.5 第四原则 · 修复：MDF 靶向功能菌群调节", REPAIR_INTRO["mdf"] +
     "\n\n路径一，紧密连接修复：关注能支持短链脂肪酸代谢的菌群及其可利用底物，评估功能菌株与益生元配合的适宜性。"
     "丁酸相关的连接组装研究提供机制依据，但“产丁酸”这一功能标签不能替代对具体菌株、剂量和人群疗效的核对。"
     "路径二，肠上皮修复：将菌株特异性的黏膜支持作用与充足蛋白、谷氨酰胺相关营养及锌等缺乏纠正相衔接，"
     "为上皮更新和黏液保护提供持续底物，避免只补菌而忽略营养不足。"
     "路径三，内毒素相关干预：从减少不利菌群扩张、支持黏膜防御及降低细菌产物刺激三个环节考虑。"
     "细胞和动物研究显示肠碱性磷酸酶（IAP）可通过去磷酸化降低LPS活性，丁酸和肠内营养与IAP表达有关；"
     "这是机制依据，不据此推定某个乳酸菌产品已经能在人体内直接结合、降解或清除内毒素。[补1,2]"
     "\n\n制剂选择：活菌、灭活菌和后生元具有不同的组成与证据，不能自动互换。免疫抑制、粒细胞减少或重症状态下，"
     "须先完成感染风险与产品安全评估，不能默认改用后生元即可安全。"
     "实际组合需核对菌株、适应证、耐受及用药相互作用，乳果糖尤其应按其临床用途单独判断。"),
    ("3.6 第五原则 · 平衡：生活方式干预", REPAIR_INTRO["lifestyle"] +
     "\n\n饮食：可采用地中海式膳食的结构，以蔬菜、水果、全谷、豆类、坚果和适宜的不饱和脂肪来源为主，"
     "减少高糖、过量高脂及超加工食品，减少酒精。成人食物来源纤维在耐受基础上逐步达到每日至少25克，"
     "增加时同步记录腹胀、排便和饮水情况。可按耐受选择食品安全合格的发酵食品，但发酵食品并不等同于具有特定疗效的益生菌制剂；"
     "免疫抑制者避免未经安全处理的发酵或生食产品。小规模成人干预研究提示发酵食品可影响菌群及部分免疫指标，效果不能外推为所有患者的修复保证。[13]"
     "\n\n运动：规律安排快走、骑行等有氧活动，并结合主要肌群的抗阻练习，形成有氧与力量训练相结合的周计划；"
     "久坐者从短时活动开始，按年龄、心肺状况及活动能力逐步增加时间和强度。结合体重、腰围和肌力观察代谢变化，避免单纯追求减重而损失肌肉；"
     "儿童、体弱或有运动禁忌者采用相应方案。出现胸痛、晕厥或明显气短时停止运动并及时评估。[补7]"
     "\n\n行为：保持稳定的入睡、起床及进餐节律，以放松训练或适宜的心理支持管理持续压力；"
     "避免不必要的非甾体抗炎药与抗生素暴露，处方药调整由医师决定。持续记录症状、饮食耐受与排便变化，"
     "定期复评时同时核对原有疾病管理、营养摄入及检测趋势，不以长期过度忌口代替病因评估。"),
)
QUALITY_CONTROL = (
    ("样本与身份核对", "核对姓名、身份标识、送检项目、样本类型和采集时间，确保样本与受检者对应并可追溯。"),
    ("样本质量与制备", "按检测方法核查接收、运输、保存与处理条件，记录溶血、脂血或其他可能影响分析的因素及实际样本状态。"),
    ("检测过程质量控制", "记录仪器和试剂状态、校准与实验室质控结果；对异常或可疑数据按流程复核，不以指标处于参考范围代替质控合格。"),
    ("结果审核与报告出具", "核对结果、单位、参考信息及来源记录，由检测机构完成检验与审核签署；报告只呈现已取得的实际记录。"),
)
QUALITY_CONTROL_NOTE = "以上为质量控制流程说明，不代表本次样本已经取得某项质控结论；实际记录以检测机构提供的信息为准。"
RISK_SCALE_TEXT = (
    "风险指数是相应评估模型中的相对分层标尺，不等同于疾病诊断或发病概率。"
    "若采用0–10分展示，分值及风险等级必须来自同一经确认的模型，不能由屏障评分直接换算，"
    "也不能将不同病种、不同来源模型的分值相互替代。未评估或空值表示现有资料尚不足以提供该项数值，"
    "不表示0分、低风险或已排除疾病。IBFC与疾病风险指数分别解释，二者不共用评分标尺。"
)
METHODS = (
    ("D-乳酸", "酶偶联比色法（通用方法介绍）", "通过酶反应产生的信号测量D-乳酸；结合代谢与临床背景，观察微生物代谢产物及通透性相关变化"),
    ("二胺氧化酶（DAO）", "酶学活性测定（通用方法介绍）", "通过底物反应评价酶活性，为肠上皮相关状态提供辅助信息；活性结果与蛋白浓度测定不能混用"),
    ("细菌脂多糖（LPS）", "动态浊度法鲎试验或酶联免疫吸附法等（通用方法介绍）", "观察内毒素相关信号；不同方法的检测对象、单位、干扰及参考范围需分别核对，不能直接互换"),
)
DISCLAIMER = (
    "本报告依据本次获得的检测及相关健康资料提供综合评估，结果不替代疾病诊断。",
    "疾病机制关联与个人风险数值属于不同概念；未评估项目不表示零风险。",
    "营养补充及微生态干预需结合适应证、过敏、用药和实际耐受；医疗措施由执业医师审核。",
    "检测信息与健康资料应妥善保管；如有持续性消化道症状或慢性疾病管理需求，请至正规医疗机构就诊。",
)


def _reference(citation, *, year=None, publisher=None, doi=None, url=None, location=None):
    return dict(citation=citation, year=year, publisher=publisher, doi=doi, url=url, location=location)


# Source numbering is preserved. Bibliographic verification and unresolved
# editorial tasks belong to the internal provenance map, not public location.
_REFERENCES = (
    _reference("Helander HF, Fändriks L. Surface area of the digestive tract — revisited.", year=2014,
               publisher="Scandinavian Journal of Gastroenterology", doi="10.3109/00365521.2014.898326",
               url="https://pubmed.ncbi.nlm.nih.gov/24694282/"),
    _reference("Sender R, Fuchs S, Milo R. Revised Estimates for the Number of Human and Bacteria Cells in the Body.",
               year=2016, publisher="PLOS Biology", doi="10.1371/journal.pbio.1002533",
               url="https://journals.plos.org/plosbiology/article?id=10.1371/journal.pbio.1002533"),
    _reference("Sturgeon C, Fasano A. Zonulin, a regulator of epithelial and endothelial barrier functions, and its involvement in chronic inflammatory diseases. Tissue Barriers. 2016;4(4):e1251384；"
               "Fasano A. Leaky gut and autoimmune diseases. Clinical Reviews in Allergy & Immunology. 2012;42(1):71–78.",
               doi="10.1080/21688370.2016.1251384", url="https://pubmed.ncbi.nlm.nih.gov/28123927/",
               location="第二篇DOI：10.1007/s12016-011-8291-x"),
    _reference("Cani PD, et al. Metabolic endotoxemia initiates obesity and insulin resistance.", year=2007, publisher="Diabetes",
               doi="10.2337/db06-1491", url="https://pubmed.ncbi.nlm.nih.gov/17456850/", location="56(7):1761–1772"),
    _reference("Erridge C, et al. A high-fat meal induces low-grade endotoxemia: evidence of a novel mechanism of postprandial inflammation.",
               year=2007, publisher="American Journal of Clinical Nutrition", doi="10.1093/ajcn/86.5.1286",
               url="https://pubmed.ncbi.nlm.nih.gov/17991637/", location="86(5):1286–1292"),
    _reference("Creely SJ, et al. Lipopolysaccharide activates an innate immune system response in human adipose tissue in obesity and type 2 diabetes.",
               year=2007, publisher="American Journal of Physiology-Endocrinology and Metabolism", doi="10.1152/ajpendo.00302.2006",
               url="https://pubmed.ncbi.nlm.nih.gov/17090751/", location="292(3):E740–E747"),
    _reference("Hotamisligil GS. Inflammation and metabolic disorders.", year=2006, publisher="Nature",
               doi="10.1038/nature05485", url="https://www.nature.com/articles/nature05485", location="444:860–867"),
    _reference("Franceschi C, et al. Inflamm-aging. An evolutionary perspective on immunosenescence. Annals of the New York Academy of Sciences. 2000;908:244–254；"
               "Franceschi C, Campisi J. Chronic inflammation (inflammaging) and its potential contribution to age-associated diseases. "
               "The Journals of Gerontology Series A. 2014;69(Suppl 1):S4–S9.",
               doi="10.1093/gerona/glu057", url="https://pubmed.ncbi.nlm.nih.gov/24833586/",
               location="第一篇DOI：10.1111/j.1749-6632.2000.tb06651.x"),
    _reference("Qi Y, et al. Intestinal Permeability Biomarker Zonulin is Elevated in Healthy Aging.", year=2017,
               publisher="Journal of the American Medical Directors Association", doi="10.1016/j.jamda.2017.05.018",
               url="https://pubmed.ncbi.nlm.nih.gov/28676292/", location="18(9):810.e1–810.e4"),
    _reference("Younossi ZM, Kalligeros M, Henry L. Epidemiology of metabolic dysfunction-associated steatotic liver disease.",
               year=2025, publisher="Clinical and Molecular Hepatology", doi="10.3350/cmh.2024.0431",
               url="https://pubmed.ncbi.nlm.nih.gov/39159948/", location="31(Suppl):S32–S50"),
    _reference("Younossi ZM, et al. The Global Epidemiology of Nonalcoholic Fatty Liver Disease and Nonalcoholic Steatohepatitis Among Patients With Type 2 Diabetes.",
               year=2024, publisher="Clinical Gastroenterology and Hepatology", doi="10.1016/j.cgh.2024.03.006",
               url="https://pubmed.ncbi.nlm.nih.gov/38521116/", location="22(10):1999–2010.e8"),
    _reference("原稿列引：肥胖流行病学资料及 IDF Diabetes Atlas, 10th ed., 2021。",
               location="原模板参考资料，第12组"),
    _reference("Wastyk HC, et al. Gut-microbiota-targeted diets modulate human immune status.", year=2021, publisher="Cell",
               doi="10.1016/j.cell.2021.06.019", url="https://pubmed.ncbi.nlm.nih.gov/34256014/"),
    _reference("原稿列引：《免疫营养素临床应用专家共识》；《中国微生态调节剂临床应用专家共识（2025版）》；国际益生菌与益生元科学协会后生元共识。",
               location="原模板参考资料，第14组"),
)

# Ordered independently from the 14 supplied source groups. These are addressed
# as [补1] etc. and inserted before per-case intervention bibliography entries.
_SUPPLEMENTAL_REFERENCES = (
    _reference("Peng L, et al. Butyrate Enhances the Intestinal Barrier by Facilitating Tight Junction Assembly via Activation of AMP-Activated Protein Kinase in Caco-2 Cell Monolayers.",
               year=2009, publisher="The Journal of Nutrition", doi="10.3945/jn.109.104638",
               url="https://pmc.ncbi.nlm.nih.gov/articles/PMC2728689/", location="139(9):1619–1625；3.3、3.5、4.1的丁酸-AMPK机制"),
    _reference("Goldberg RF, et al. Intestinal alkaline phosphatase is a gut mucosal defense factor maintained by enteral nutrition.",
               year=2008, publisher="Proceedings of the National Academy of Sciences", doi="10.1073/pnas.0712140105",
               url="https://pmc.ncbi.nlm.nih.gov/articles/PMC2265168/", location="105(9):3551–3556；3.5的IAP与LPS机制"),
    _reference("Marchiando AM, et al. Caveolin-1–dependent occludin endocytosis is required for TNF-induced tight junction regulation in vivo.",
               year=2010, publisher="Journal of Cell Biology", doi="10.1083/jcb.200902153",
               url="https://pmc.ncbi.nlm.nih.gov/articles/PMC2854371/", location="189(1):111–126；4.4的MLCK与连接调节"),
    _reference("NICE. Nutrition support for adults: oral nutrition support, enteral tube feeding and parenteral nutrition (CG32).",
               year=2017, publisher="National Institute for Health and Care Excellence",
               url="https://www.nice.org.uk/guidance/cg32/chapter/recommendations", location="1.3、1.6–1.8节；3.3营养途径及适用条件"),
    _reference("Peery AF, et al. AGA Clinical Practice Guideline on Fecal Microbiota-Based Therapies for Select Gastrointestinal Diseases.",
               year=2024, publisher="Gastroenterology", doi="10.1053/j.gastro.2024.01.008",
               url="https://gastro.org/clinical-guidance/fecal-microbiota-based-therapies-for-select-gastrointestinal-diseases/",
               location="166(3):409–434；3.4移植适应证与免疫状态"),
    _reference("World Health Organization. Healthy diet.", publisher="World Health Organization",
               url="https://www.who.int/news-room/fact-sheets/detail/healthy-diet", location="膳食纤维与食物结构；3.3、3.6"),
    _reference("World Health Organization. WHO guidelines on physical activity and sedentary behaviour.", year=2020,
               publisher="World Health Organization", url="https://www.who.int/publications/i/item/9789240015128",
               location="分年龄人群建议；3.6有氧、抗阻与减少久坐"),
    _reference("Braniste V, et al. The gut microbiota influences blood-brain barrier permeability in mice.", year=2014,
               publisher="Science Translational Medicine", doi="10.1126/scitranslmed.3009759",
               url="https://pubmed.ncbi.nlm.nih.gov/25411471/", location="6(263):263ra158；4.5肠–脑轴动物机制研究"),
)


def references():
    return deepcopy(list(_REFERENCES))


def supplemental_references():
    """Return stable, patient-independent support for the restored fixed copy."""
    return deepcopy(list(_SUPPLEMENTAL_REFERENCES))
