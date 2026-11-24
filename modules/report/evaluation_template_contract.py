"""Patient-facing cell responsibilities shared by report planning and review.

The approved seven-chapter HTML is a view of this contract, not a source of
patient facts or reusable case paragraphs. A result column and its adjacent
interpretation deliberately have different jobs.
"""
from __future__ import annotations

from copy import deepcopy


TEMPLATE_REVISION = 'seven-chapter-topic-aligned-v1'
CHAPTERS = (
    ('healthOverview', '健康状况总览', '先给整体判断和处理先后顺序，连接主要问题与共同可调整因素。'),
    ('medicalAssessment', '医学评估', '按实际疾病及健康问题整合检查和问答，分析影响与个人诱因。'),
    ('microbiomeAssessment', '肠道菌群与肠道功能评估', '结果栏保留检测内容，健康意义栏说明个人健康提示与调整方向。'),
    ('diseaseRiskAssessment', '六大类疾病风险评估', '结合全部资料逐问题说明相关表现、风险性质和改善安排。'),
    ('nutritionAssessment', '个体营养评估', '按营养功能组评价整体格局及具体膳食调整，不把功能预测当人体缺乏。'),
    ('functionalPathways', '肠道菌群功能通路分析评估', '将各通路与当前健康问题联动，说明影响及可执行的改善方向。'),
    ('comprehensiveConclusion', '综合结论', '收束为下一阶段落实的行动、目的和时间或触发条件。'),
)

REPORT_MEDICAL_SAFETY = [
    '需要诊断性查因的实际异常与生活管理并行；不能把先调饮食/运动再复查当作必要查因的替代，亦不能由阳性筛查直接命名癌症高风险。',
    '生活习惯、检查线索、菌群研究关联和已确诊疾病性质不同；不能把未来风险或单项C肽/胰岛素变化写成已存在胰岛素抵抗或糖尿病。',
    '实验室参考范围与临床筛查/诊断界值不同；结合肥胖、家族史及糖代谢线索评估时，不能仅用参考范围内一句话排除合理的近期筛查安排。',
    '不能从菌群功能低项倒推蔬菜、水果、蛋白质或膳食纤维摄入不足；饮食事实必须来自真实问答，已有良好搭配要保留。',
    '菌群相关环境化合物转化、维生素或神经递质通路不等于人体清除毒物、营养或脑内递质实测，不能据此解释全身疲劳、感染或特定疾病。',
    '单次影像不能写病灶长期稳定/保持不变；重复读取同一原报告不构成随访复测。无需处理、固定年复查等个体安排必须有相称依据。',
    '健康目标合入管理安排，不当作新疾病或单独风险行；原来源中高风险分档不得自动升级为本人的风险等级。',
    '章内小结与总览应讲健康提示和改善顺序，不报几个正常几个异常或凭记忆计数；如需提数量，只用本章实际完整结果清单核对。',
    '多种问题可以共用饮食、体重或活动管理，不等于它们有相同病因。疾病发生机制、共同致病因素及菌种的个人致病/保护解释属于医学关联命题，必须有适用原文，不能仅凭同现写成临床归纳。',
    '单项检验正常不能直接推断器官炎症轻微、病灶良性或排除相关疾病；引用只支持所给完整原段实际覆盖的命题，不可为同句额外结论背书。',
    '将适用指南落实成相称的日常计划可以个体化；自行安排的频次与时长应作为建议表达，不能写成文献已验证该患者的具体剂量或效果。',
    '检测栏目名称不构成基因功能身份验证；只有确切基因或序列记录时，不借栏目标签推定已检出某毒素、致病蛋白或活动性感染。功能身份需有准确注释支持。',
    '菌群比例、失调指数及预测年龄不能直接证实或排除个人代谢疾病、全身免疫异常或提前衰老；原结果如实保留，个人管理依据实际症状、临床信息与适用证据。',
    '个人症状和摄入情况要有对应的实际回答：疲劳不等于情绪异常，吃甜食或夜宵频繁不等于已挤占蔬果、蛋白质摄入；没有份量资料不编造食物不足或替代关系。',
    '同一人在各章的问题名称和既有临床分类保持一致；不能在一处采用已记录肥胖诊断、另一处又按未声明的其他标准称为超重，也不能以栏目所属方向创造新主诉。',
    '物种或分子的生理属性不等于检测丰度变化的临床方向；例如具有稳态相关作用不能证明丰度升高代表更健康。原文未评价增减方向时，不给个人高低值附加有益、有害或风险变化结论。',
    '生活习惯可作为个人相关背景及改善依据，不据同现直接证明本人的致病因果；用相称的关联和管理意义表达，不宣称某行为已经造成当前疾病。',
    '同一临床问题的处置顺序全篇一致：反复隐血等查因事项与便秘/痔病管理同步，不能等待生活调整或复测仍阳性才安排消化科，也不能据痔病推定出血来自肛周而非肠道。保留原隐血检验名称，不擅自改称FIT。',
    '突发单侧无力、口角歪斜、言语不清等急性神经症状应立即急救/急诊；不能写成普通专科门诊、等待观察或常规复查。',
    '正常、检出与未检出是不同状态。仅原文明确未检出时可如此表述；正常等级或非零但正常的丰度不能改写为未检出，也不能由面板未检出排除临床感染。',
    '实际用药背景不是用药建议，不能因目前没用药就建议保持不治疗。没有食物摄入量则不判定已经足量或无需加量；数值目标和执行计划在各章保持同一阶段口径。',
    '所有建议逐项匹配明确的性别、年龄及实际生理状态；性激素项目不改变患者性别。没有相应生理状态时，不生成月经、妊娠等专属采样或管理安排。',
    '菌群功能与膳食、症状可以共用于选择管理方向，但不能据此宣称功能弱项由本人的主食或蛋白质分配造成、人体吸收利用不足、或与疲劳相呼应；本人的这些关系须有独立适用依据。',
]

PATIENT_TEXT_GUIDANCE = [
    '交付给读者的是已经形成的管理结论、与本人的关联及行动，不是工作过程、资料说明或医学边界讲解。限制条件只用于内部判断。',
    '某个推断缺少依据时，删除该推断，改写为已有依据的管理重点。不能用一大段检测描述再接不等于、不能说明、尚不支持或仍需明确代替结论；也不能删掉限定后反而写成确诊。',
    '结果和状态留在相邻数据列；分析列从健康影响或改善重点起笔。正常固定项目用简短维护结论，不列正常清单，不制造新问题。',
    '关注程度以处理先后和实际影响表达。准确问题名或必要短定语承载疾病性质，不在每一行反复解释预测与临床的区别。',
    '各模块选择不同、与该模块相关的行动；具体量、频次、阶段目标集中到末章行动表，前文不要反复抄同一套甜食、夜宵和快走方案。',
    '直接给出做什么及目的；确有查因需要时写清专科、带什么资料及何时处理，不用需要进一步明确作空泛收尾。',
    '正常或相反资料用于内部综合判断；正文不反复列没有哪些症状、不支持哪些疾病来填篇幅。确需提醒的变化写入具体行动触发条件。',
]


def topic_guidance(owner):
    """Give each functional/nutrient topic its own patient-facing purpose."""
    code = str(owner.get('category') or owner.get('code') or '')
    focus = {
        'P1': '主食粗细搭配、减少额外糖与睡前加餐，结合实际糖代谢管理。',
        'carbohydrateMetabolism': '主食搭配和额外糖的具体调整，不把所有碳水化合物一概减少。',
        'P2': '脂肪来源和烹调方式的替换，结合实际血脂管理，不把通路变化当成血脂病因。',
        'lipidMetabolism': '脂肪来源与烹调方式，减少油炸肥肉、按实际膳食选择替换。',
        'P3': '膳食纤维种类、饮水与排便耐受，循序调整，关注便形、用力及腹胀。',
        'P4': '蛋白质食物的选择和三餐分配，按实际食量调整，不从食物种类推断足量。',
        'aminoAcidsAndMetabolites': '鱼蛋豆等蛋白质食物的正餐分配与烹调，不臆测实际摄入量。',
        'P5': '食物类别和一周搭配多样性，给具体食物轮换方向。',
        'vitamins': '食物类别和日常搭配的多样性，按实际饮食给具体调整。',
        'P6': '围绕实际疲劳、作息与白天活动提出恢复安排，不解释为脑内递质问题。',
        'neurotransmitters': '实际睡眠、精力恢复与日间活动；不要另造情绪或神经主诉。',
        'P7': '没有相关临床问题时，简短给出维持常规膳食的结论，不额外引出低嘌呤或核苷酸方案。',
        'P8': '蔬果颜色与种类的轮换，不假设甜食已经挤占蔬果摄入。',
        'naturalPigments': '在实际饮食基础上轮换深绿、橙黄等颜色的蔬果种类。',
        'pigments': '在实际饮食基础上轮换深绿、橙黄等颜色的蔬果种类。',
        'nucleotideMetabolism': '没有相关临床问题时维持常规膳食，不额外提出低嘌呤饮食或补充方案。',
        'toxicSubstanceDegradation': '仅据实际暴露资料给减少暴露的方向，没有暴露记录不造体内蓄积或排毒问题。',
        'P9': '围绕实际肠道表现、食品卫生及症状变化提出处理。DNA检出对象按真实基因名，勿推定出血病因或活动性感染；必要查因与日常排便管理并行。',
        'P10': '按实际耐药结果和暴露资料给用药沟通或减少暴露的方向，不造排毒/免疫问题；正常结果不改称未检出。',
        'harmfulMetabolites': '有实际临床问题才提出对应调整；正常时简短维护日常饮食，不额外制造毒素负担。',
    }
    return focus.get(code)

_AVOID = [
    '把旁栏的原值、参考范围或正常异常计数换句话重抄一遍',
    '先说发现A，再说A异常，再建议关注A的循环表达',
    '来源记录、原级、资料覆盖、模型流程、评分流程、检索或审查过程',
    '第一人称本人/患者自称；通篇统一使用您或省略人称',
    '将相关性写成已证实的个人病因、将检测关联写成确诊',
    '一律泛写均衡饮食适量运动，未连接本项问题及实际个人习惯',
    '用没有直接证据、不能解释或未确诊等空结论占据解读；问题性质以简短定语交代，不在每行重复局限说明',
    '检测做了什么、涉及哪些通路、预测是什么等过程性铺垫；以及用不等于体内缺乏、不能代表人体功能作结尾',
    '原模型分值/原分档、核验状态与来源说明；以及唯一、全部均由等超出真实判断的绝对化概括',
]


def cell_contract(path):
    """Return a semantic contract for an existing public JSON text address."""
    p = tuple(path)
    section, field = p[0], p[-1]
    common = dict(avoid=deepcopy(_AVOID), patientTextGuidance=deepcopy(PATIENT_TEXT_GUIDANCE), structure=[
        '先给本栏负责的管理结论', '只说明有依据的个人关联或影响', '收束为本项最优先的行动'], maxChars=220)
    if section == 'healthOverview':
        if 'keyFindings' in p:
            row = dict(label='核心提示', question='针对这个实际健康问题，您现在应先做什么、为何值得优先处理？',
                structure=['一句话给优先行动', '点出个人诱因或健康影响', '必要时补时间或警示条件'], maxChars=140)
        elif 'summaries' in p:
            row = dict(label='三类评估结论摘要', question='该领域对整体健康管理增加了什么有用结论？',
                structure=['领域整体结论', '连接已知个人问题与可调整因素', '指出本领域最值得落实的做法'], maxChars=260)
        elif 'assessmentChart' in p:
            row = dict(label='维度关注重点', question='本维度最值得处理的具体问题和安排是什么？', maxChars=90)
        elif 'overallScore' in p:
            row = dict(label='主要问题之间的联系', question='哪些问题有共同的管理环节，哪些应单独安排？', maxChars=220)
        else:
            row = dict(label='综合评估总览', question='读完整份报告，您应记住的整体判断和先后顺序是什么？',
                structure=['总述主要问题与轻重缓急', '分述共同诱因及可能影响', '收束为近期和持续管理的重点'], maxChars=480)
    elif section == 'medicalAssessment':
        if 'questionnaireSummary' in p:
            row = dict(label='主诉解读',
                question='对已记录的具体不适，哪些实际情境或检查能帮助理解？接下来最值得观察或处理什么？',
                structure=['概括困扰及有意义的个人情境', '解释有对应依据的关联', '说明下一步的目的'], maxChars=230)
        elif 'triggerAnalysis' in p:
            if field == 'evidenceStrength':
                row = dict(label='证据强度', question='本条个人行为与健康问题的关联证据强度如何？', allowedValues=['强', '中', '弱'], maxChars=1)
            elif field == 'modifiability':
                row = dict(label='可干预性', question='本条因素在日常管理中可调整程度如何？', allowedValues=['高', '中', '低'], maxChars=1)
            else:
                row = dict(label='与当前健康问题的关联分析', question='这个实际习惯如何关联当前问题，您具体先改什么？',
                    structure=['直接点出与个人问题的关系', '说明可调整环节', '给一个适合该习惯的具体做法'], maxChars=200)
        elif 'triggerConclusion' in p:
            row = dict(label='生活习惯与健康背景小结', question='综合所有诱因，最值得同时调整哪几件事？', maxChars=260)
        elif field == 'sourceAdvice':
            row = dict(label='改善安排', question='本问题现在具体怎样处理，目的和时机是什么？不要再次解释旁栏的病情与数据。',
                structure=['直接给行动及目的', '只在必要时说明时机或急诊条件'], maxChars=170)
        else:
            row = dict(label='健康影响与改善方向', question='这个疾病或健康问题目前意味着什么、会影响什么，您先如何改善？',
                structure=['当前问题和严重程度', '个人诱因与健康影响', '与问题相称的改善或就医方向'], maxChars=220)
    elif section == 'microbiomeAssessment':
        row = dict(label='健康意义', question='本项对您的肠道健康和管理有什么实际提示，如何调整？',
            structure=['整体健康提示而非再报数值', '与实际排便、膳食或已知问题的有据联系', '对应的食物/作息/症状管理做法'], maxChars=210)
        if 'antimicrobialResistance' in p or field == 'resistanceNotice':
            row.update(question='本次耐药相关结果对日常用药沟通有什么实际意义？', maxChars=150)
        elif 'scfaInterpretation' in p:
            row.update(label='短链脂肪酸综合解读', question='整组发酵相关功能有什么特点，应怎样搭配食物和照顾排便耐受？', maxChars=240)
        elif 'structureInterpretation' in p:
            row.update(question='综合菌群结构，您的日常食物搭配和肠道耐受管理最应关注什么？', maxChars=300)
        elif 'keyMicrobialSignals' in p:
            row.update(label='异常菌群综合提示', question='异常菌群组合可能关联哪些实际表现，您日常应怎样留意和调整？不重复结构总结。', maxChars=300)
    elif section == 'diseaseRiskAssessment':
        if 'riskRanking' in p:
            row = dict(label='解读与改善方向', question='这个具体问题最值得您采取什么行动，与哪些实际个人因素相关，希望改善什么表现？',
                structure=['从本问题的管理重点起笔，以准确措辞体现性质', '只保留与本问题有关的个人关联', '给日常优先行动及必要的就医触发条件'], maxChars=230)
        elif field == 'attentionBasis':
            row = dict(label='本次关注重点', question='本类当前管理重点用一个具体短语如何概括？', maxChars=24)
        elif 'overviewMatrix' in p:
            row = dict(label='整体解读', question='本类当前最先落实的一件事是什么？用简短行动与目的概括，不复述证据或具体运动处方。',
                structure=['一句本类优先事项与行动'], maxChars=100)
        else:
            row = dict(label='本类综合解读', question='综合本类全部问题，有什么共同诱因、整体影响和管理顺序？',
                structure=['本类总体结论和主要矛盾', '共同个人诱因与影响', '集中处理顺序，不重复逐行段落'], maxChars=270)
    elif section == 'nutritionAssessment':
        row = dict(label='判定与提示', question='本组功能整体有什么特点，结合您的饮食和健康问题，膳食应如何调整？',
            structure=['本组整体功能特点，不计数报分', '与已知个人问题的合理健康提示', '食物种类、搭配或进食安排的实际调整'], maxChars=220)
        if p[1] in {'nutritionInterpretation', 'aminoMetaboliteInterpretation', 'neurotransmitterInterpretation'}:
            row.update(label='营养功能综合提示', maxChars=280)
    elif section == 'functionalPathways':
        row = dict(label='健康意义与联动', question='本通路整体与您的哪些健康问题相关，改善重点是什么？',
            structure=['通路整体健康提示', '与真实个人问题和诱因的有据联系', '相应改善方向，避免重复别的通路'], maxChars=210)
        if field == 'text':
            row.update(label='功能通路小结', question='各通路共同指向哪些可落实的改善环节？', maxChars=280)
    else:
        if 'nextSteps' in p or field == 'followupAction':
            row = dict(label='下一步、目的及时间/触发条件', question='这一阶段具体做什么、为解决什么问题、何时或什么情况下执行？',
                structure=['行动', '目的', '时间或症状触发条件'], maxChars=230)
        else:
            row = dict(label='综合结论', question='把前文判断收束为未来一阶段的执行安排，而不是再次列疾病和指标。',
                structure=['总述管理目标', '分述饮食、活动、排便与必要诊治安排', '收束为落实顺序和观察目标'], maxChars=450)
    result = common | row
    if (section in {'microbiomeAssessment', 'nutritionAssessment', 'functionalPathways'}
            or section == 'healthOverview' and 'summaries' in p
            and any(name in p for name in ('microbiome', 'nutrition'))):
        result['analysisMode'] = 'management_direction'
        result['structure'] = ['直接说明本项实际管理重点', '给与本项相关的做法及个人执行目标']
        result['question'] += (' 本栏写管理结论，不再报告检测高低或解释预测边界。'
            'clinical_synthesis只写相称的行动句；确有原文的医学机制另以evidence解释，不推断本人的炎症、免疫、衰老或营养实测状态。')
    if result.get('allowedValues'):
        result.update(form='classification', structure=['只返回allowedValues中的一个字'],
            question=result['question'] + '这是栏目评定，不写医学段落、不需给该汉字另找患病证据。')
    elif (section == 'diseaseRiskAssessment' and field == 'attentionBasis'):
        result.update(form='label', structure=['返回一个具体管理重点短语，不写栏目名，不写整段'], maxChars=24)
    else:
        result['form'] = 'paragraph'
    return result
