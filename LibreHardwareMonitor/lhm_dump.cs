using System;
using System.Collections.Generic;
using System.Globalization;
using LibreHardwareMonitor.Hardware;

public class UpdateVisitor : IVisitor
{
    public void VisitComputer(IComputer computer) { computer.Traverse(this); }
    public void VisitHardware(IHardware hardware)
    {
        hardware.Update();
        foreach (var sh in hardware.SubHardware) sh.Accept(this);
    }
    public void VisitSensor(ISensor sensor) { }
    public void VisitParameter(IParameter parameter) { }
}

public class Program
{
    static string J(string s) { return s.Replace("\\", "\\\\").Replace("\"", "\\\""); }

    static void Emit(IHardware h, List<string> items)
    {
        foreach (var s in h.Sensors)
        {
            if (s.Value.HasValue)
            {
                items.Add(string.Format(
                    "{{\"hw\":\"{0}\",\"htype\":\"{1}\",\"stype\":\"{2}\",\"name\":\"{3}\",\"value\":{4}}}",
                    J(h.Name), h.HardwareType, s.SensorType, J(s.Name),
                    s.Value.Value.ToString("R", CultureInfo.InvariantCulture)));
            }
        }
    }

    public static void Main()
    {
        var computer = new Computer
        {
            IsCpuEnabled = true,
            IsGpuEnabled = true,
            IsMemoryEnabled = true,
            IsMotherboardEnabled = true,
        };
        computer.Open();
        computer.Accept(new UpdateVisitor());

        var items = new List<string>();
        foreach (var hw in computer.Hardware)
        {
            Emit(hw, items);
            foreach (var sh in hw.SubHardware) Emit(sh, items);
        }

        Console.WriteLine("[" + string.Join(",", items) + "]");
        computer.Close();
    }
}
