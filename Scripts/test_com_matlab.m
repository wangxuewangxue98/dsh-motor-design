% Motor-CAD COM 连接测试脚本
% 尝试通过 actxserver 连接 Motor-CAD

try
    % 尝试创建 COM 对象
    fprintf('尝试 actxserver(''MotorCAD.AppAutomation'')...\n');
    mcad = actxserver('MotorCAD.AppAutomation');
    fprintf('COM 连接成功!\n');
    
    % 获取一些基本信息
    try
        [res, val] = invoke(mcad, 'GetVariable', 'MotorCADprocessID');
        fprintf('MotorCADprocessID = %s\n', val);
    catch e
        fprintf('GetVariable MotorCADprocessID 失败: %s\n', e.message);
    end
    
    try
        [res, val] = invoke(mcad, 'GetVariable', 'MotorCADexeName');
        fprintf('MotorCADexeName = %s\n', val);
    catch e
        fprintf('GetVariable MotorCADexeName 失败: %s\n', e.message);
    end
    
    try
        [res, val] = invoke(mcad, 'GetVariable', 'CurrentExeDir_MotorLAB');
        fprintf('CurrentExeDir_MotorLAB = %s\n', val);
    catch e
        fprintf('GetVariable CurrentExeDir_MotorLAB 失败: %s\n', e.message);
    end
    
    % 尝试获取转速
    try
        [res, speed] = invoke(mcad, 'GetVariable', 'Shaft_Speed_[RPM]');
        fprintf('Shaft_Speed = %f\n', speed);
    catch e
        fprintf('GetVariable Shaft_Speed 失败: %s\n', e.message);
    end
    
    % 尝试运行稳态热分析
    fprintf('\n尝试 DoSteadyStateAnalysis...\n');
    try
        invoke(mcad, 'DoSteadyStateAnalysis');
        fprintf('DoSteadyStateAnalysis 完成!\n');
        
        % 获取温度结果
        [res, temp_winding] = invoke(mcad, 'GetVariable', 'T_[Winding_Average]');
        fprintf('T_[Winding_Average] = %f\n', temp_winding);
        
        [res, temp_housing] = invoke(mcad, 'GetVariable', 'T_[Housing_Average]');
        fprintf('T_[Housing_Average] = %f\n', temp_housing);
        
        % 保存结果到文件
        fid = fopen('D:\MotorDesign\Scripts\matlab_com_results.txt', 'w');
        fprintf(fid, 'COM连接: 成功\n');
        fprintf(fid, 'DoSteadyStateAnalysis: 成功\n');
        fprintf(fid, 'T_[Winding_Average] = %f\n', temp_winding);
        fprintf(fid, 'T_[Housing_Average] = %f\n', temp_housing);
        fclose(fid);
        fprintf('结果已保存到 matlab_com_results.txt\n');
    catch e
        fprintf('DoSteadyStateAnalysis 失败: %s\n', e.message);
        
        % 保存错误信息
        fid = fopen('D:\MotorDesign\Scripts\matlab_com_results.txt', 'w');
        fprintf(fid, 'COM连接: 成功\n');
        fprintf(fid, 'DoSteadyStateAnalysis 失败: %s\n', e.message);
        fclose(fid);
    end
    
    % 退出 Motor-CAD COM
    invoke(mcad, 'Quit');
    fprintf('COM 连接已关闭\n');
    
catch e
    fprintf('actxserver 失败: %s\n', e.message);
    
    % 尝试小写 ProgID
    try
        fprintf('\n尝试 actxserver(''motorcad.appautomation'')...\n');
        mcad = actxserver('motorcad.appautomation');
        fprintf('COM 连接成功 (小写 ProgID)!\n');
        invoke(mcad, 'Quit');
    catch e2
        fprintf('小写 ProgID 也失败: %s\n', e2.message);
    end
    
    % 尝试 GetActiveObject (连接到已运行实例)
    try
        fprintf('\n尝试 actxGetRunningServer...\n');
        mcad = actxGetRunningServer('MotorCAD.AppAutomation');
        fprintf('GetRunningServer 成功!\n');
        invoke(mcad, 'Quit');
    catch e3
        fprintf('GetRunningServer 失败: %s\n', e3.message);
    end
    
    % 保存错误信息
    fid = fopen('D:\MotorDesign\Scripts\matlab_com_results.txt', 'w');
    fprintf(fid, 'COM连接: 失败\n');
    fprintf(fid, '错误: %s\n', e.message);
    fclose(fid);
end

fprintf('\n脚本执行完毕\n');
